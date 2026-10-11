"""Offline regressions for publication, recovery, page boundaries, and running jobs."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentale.config import Settings
from opentale.domain import Limits
from opentale.growth_api import register_graph_routes
from opentale.growth_kernel import add_link, add_node, navigate
from opentale.growth_runner import GrowthRunner, identifier
from opentale.kernel import KernelError
from opentale.providers.base import ModelResult
from opentale.providers.growth import DeepSeekGrowthProvider, FixtureGrowthProvider, validate_decision

KEY = "offline-boundaries-dummy-key"
WINDOWS = [
    {"id": "door", "label": "门前", "seed": "她站在门前。", "direction": "接住门前的回应"},
    {"id": "stairs", "label": "台阶", "seed": "他停在台阶下。", "direction": "接住台阶下的回应"},
]


@pytest.fixture
def runner(tmp_path):
    runtime = GrowthRunner(tmp_path, Settings(api_key=KEY))
    yield runtime
    runtime.store.close()


def create(runner, **changes):
    return runner.create(title="边界回归", provider="fixture", windows=WINDOWS,
                         limits=Limits(max_calls=None, max_tokens=None, max_windows=None), **changes)


def own_rows(runner, run_id, kind, scope):
    return [row for row in runner.rows(runner.load(run_id), kind, scope) if row.get("scope_id") == scope]


def graph_app(runner):
    # This API never loads local configuration or the legacy preset library.
    app = FastAPI()

    @app.exception_handler(KernelError)
    async def safe_kernel_error(_, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=422)

    register_graph_routes(app, runner, None)
    return app


@pytest.mark.parametrize("field", ["title", "preset_id", "window_id", "label"])
def test_entire_creation_payload_rejects_current_key_before_persistence(runner, field):
    payload = {"title": "离线", "provider": "fixture", "windows": [dict(row) for row in WINDOWS]}
    if field == "window_id":
        payload["windows"][0]["id"] = KEY
    elif field == "label":
        payload["windows"][0]["label"] = KEY
    else:
        payload[field] = KEY
    with pytest.raises(KernelError) as captured:
        runner.create(**payload)
    assert captured.value.code == "CONTENT_UNSAFE" and KEY not in str(captured.value)
    assert not runner.store.list_runs()
    assert runner.store._db.execute("SELECT COUNT(*) FROM texts").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["title", "direction", "entry_node_ids"])
def test_reading_payload_rejects_key_without_partial_method_or_track(runner, field):
    run_id = create(runner)["run"]["id"]
    before = runner.store.count_objects(run_id, "method")
    payload = {"title": "读轨", "direction": "继续阅读"}
    payload[field] = [KEY] if field == "entry_node_ids" else KEY
    with pytest.raises(KernelError) as captured:
        runner.add_reading(run_id, **payload)
    assert captured.value.code == "CONTENT_UNSAFE" and KEY not in str(captured.value)
    assert not runner.load(run_id).reading_tracks
    assert runner.store.count_objects(run_id, "method") == before


def test_snapshot_rechecks_material_against_reloaded_credential(runner):
    runner.settings = Settings()
    run_id = runner.create(title=KEY, provider="fixture", windows=WINDOWS)["run"]["id"]
    runner.settings = Settings(api_key=KEY)
    with pytest.raises(KernelError) as captured:
        runner.snapshot(run_id)
    assert captured.value.code == "CONTENT_UNSAFE" and KEY not in str(captured.value)


def test_preset_reads_and_import_recheck_reloaded_credential(runner):
    runner.settings = Settings()
    app = graph_app(runner)
    app.state.graph_presets.save({"id": "review", "name": "离线预设", "windows": [
        {"id": "door", "label": "门前", "seed": KEY, "direction": ""},
    ], "shared_seed": ""})
    runner.settings = Settings(api_key=KEY)
    with TestClient(app) as client:
        catalog = client.get("/api/graph-presets")
        assert catalog.status_code == 200 and KEY not in catalog.text
        assert not [preset for preset in catalog.json()["presets"] if preset["origin"] == "custom"]
        for response in (client.get("/api/graph-presets/review"),
                         client.post("/api/graph-runs", json={"preset_id": "graph:review"})):
            assert response.status_code == 422 and KEY not in response.text
            assert response.json()["error"]["code"] == "CONTENT_UNSAFE"
    assert not runner.store.list_runs()


@pytest.mark.parametrize("collection", ["list", "node"])
def test_raw_graph_read_routes_recheck_material_against_reloaded_key(runner, collection):
    runner.settings = Settings()
    snapshot = runner.create(title=KEY, provider="fixture", windows=[{"id": "door", "seed": KEY}])
    runner.settings = Settings(api_key=KEY)
    path = "/api/graph-runs"
    if collection == "node":
        path += f"/{snapshot['run']['id']}/nodes/{snapshot['nodes'][0]['id']}"
    with TestClient(graph_app(runner)) as client:
        response = client.get(path)
    assert response.status_code == 422 and KEY not in response.text
    assert response.json()["error"]["code"] == "CONTENT_UNSAFE"


@pytest.mark.parametrize("selected_private", [True, False])
async def test_publication_taints_actual_private_sources_but_not_unread_catalog(runner, monkeypatch,
                                                                              selected_private):
    snapshot = create(runner)
    run_id, scope = snapshot["run"]["id"], "window:door"
    public = snapshot["windows"][0]["entry_node_ids"][0]
    state = add_node(runner.store, runner.load(run_id), "door", "她记得只在这里说过的私密约定。",
                     private_scope=scope, request_id="private-source")
    private = state.windows["door"].working_node_id
    if not selected_private:
        state.windows["door"].working_node_id = public
        runner.store.save_head(state)

    class PublicationFixture(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["work_seq"] == 0:
                decision = {"action": "try_write", "args": {"parent_ids": [public]}}
            else:
                decision = {"action": "publish", "args": {"trial_id": frame["pending_trial"]["id"],
                                                            "parent_ids": [public]}}
            return ModelResult(validate_decision(frame, decision))

        async def draft(self, frame, *, repair=None):
            actual = {source["id"] for source in frame["sources"]}
            assert (private in actual) is selected_private
            preview = next(row for row in frame["nodes"] if row["id"] == private)
            assert preview["text_preview"] == ""
            return ModelResult("她把只在这里说过的约定写进门前的生活。" if selected_private else "她推开了门。")

    monkeypatch.setattr(runner, "provider", lambda state: PublicationFixture())
    await runner.step_scope(run_id, scope)
    await runner.step_scope(run_id, scope)
    state = runner.load(run_id)
    node_id = state.windows["door"].working_node_id
    published = runner.store.get(run_id, "node", node_id, scope_id=scope)
    assert bool(published["private_scope"]) is selected_private
    assert (runner.store.get(run_id, "node", node_id) is None) is selected_private
    assert (runner.store.get(run_id, "node", node_id, scope_id="window:stairs") is None) is selected_private
    spans = published["source_spans"]
    assert (private in {span["node_id"] for span in spans}) is selected_private
    if selected_private:
        exact = runner.store.get(run_id, "node", private, scope_id=scope)
        private_span = next(span for span in spans if span["node_id"] == private)
        assert private_span["node_version"] == exact["version"]
        assert private_span["text_ref"] == exact["text_ref"]


@pytest.mark.parametrize("startup_recovery", [False, True])
async def test_interrupted_request_reservation_settles_once_before_retry(runner, monkeypatch, startup_recovery):
    run_id, scope = create(runner)["run"]["id"], "window:door"
    state = runner.load(run_id)
    frame = runner.frame(state, scope, runner.session(state, scope))
    request_id = f"request-{identifier(scope, 1, 'decide')}"
    estimate = 3565
    projection = FixtureGrowthProvider().projection(frame, "decide")
    state.config["usage"].update(calls=1, reserved_tokens=estimate)
    with runner.store.transaction():
        runner.put(run_id, "request", {"id": request_id, "scope_id": scope, "private_scope": scope,
            "stage": "decide", "frame": frame, "projection": projection, "status": "calling",
            "reserved_tokens": estimate, "attempts": [], "graph_commit": state.graph_commit})
        runner.store.save_head(state)

    class BilledFixture(FixtureGrowthProvider):
        calls = 0

        async def decide(self, frame, *, repair=None):
            self.calls += 1
            return ModelResult({"action": "wait", "args": {}}, 20, 5)

    provider = BilledFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    if startup_recovery:
        runner.recover()
        runner.recover()
    value, _ = await runner.call(run_id, scope, 1, "decide", frame)
    usage = runner.load(run_id).config["usage"].copy()
    assert usage == {"calls": 2, "failed_calls": 1, "input_tokens": 20, "output_tokens": 5,
                     "unknown_tokens": estimate, "reserved_tokens": 0}
    assert await runner.call(run_id, scope, 1, "decide", frame) == (value, request_id)
    assert runner.load(run_id).config["usage"] == usage and provider.calls == 1
    request = runner.store.get(run_id, "request", request_id, include_private=True)
    assert [attempt["status"] for attempt in request["attempts"]] == ["interrupted", "accepted"]


async def test_request_and_initial_reservation_roll_back_together(runner, monkeypatch):
    run_id, scope = create(runner)["run"]["id"], "window:door"
    state = runner.load(run_id)
    frame = runner.frame(state, scope, runner.session(state, scope))
    save_head = runner.store.save_head

    def fail_before_head_save(state):
        if state.config["usage"]["calls"]:
            raise KernelError("TEST_INTERRUPTED", "离线提交中断。")
        return save_head(state)

    monkeypatch.setattr(runner.store, "save_head", fail_before_head_save)
    with pytest.raises(KernelError, match="离线提交中断"):
        await runner.call(run_id, scope, 1, "decide", frame)
    assert runner.load(run_id).config["usage"]["calls"] == 0
    assert runner.load(run_id).config["usage"]["reserved_tokens"] == 0
    assert not runner.rows(runner.load(run_id), "request", owner=True)
    assert not runner.rows(runner.load(run_id), "session", owner=True)


async def test_accepted_response_installation_replay_deduplicates_feedback(runner, monkeypatch):
    run_id, scope = create(runner)["run"]["id"], "window:door"
    for _ in range(3):
        await runner.step_scope(run_id, scope)
    execute, interrupted = runner.execute, False

    async def interrupt_after_response(*args):
        nonlocal interrupted
        result = await execute(*args)
        if args[3]["action"] == "respond" and not interrupted:
            interrupted = True
            raise KernelError("TEST_INTERRUPTED", "已保存反馈等待收据。")
        return result

    monkeypatch.setattr(runner, "execute", interrupt_after_response)
    with pytest.raises(KernelError, match="等待收据"):
        await runner.step_scope(run_id, scope)
    usage = runner.load(run_id).config["usage"].copy()
    await runner.step_scope(run_id, scope)
    assert runner.load(run_id).config["usage"] == usage
    trial = own_rows(runner, run_id, "trial", scope)[0]
    responses = own_rows(runner, run_id, "response", scope)
    assert len(responses) == len(trial["feedback_refs"]) == 1
    assert [row["action"] for row in own_rows(runner, run_id, "receipt", scope)].count("respond") == 1


@pytest.mark.parametrize("range_args,expected_range", [({}, (0, 2500)), ({"start": 2500, "end": 5000}, (2500, 5000))])
async def test_long_node_renders_only_the_actual_bounded_visit(runner, monkeypatch, range_args, expected_range):
    text = "她走向门前。" * 20_000
    snapshot = runner.create(title="长原文", provider="fixture", windows=[{"id": "door", "seed": text}])
    run_id, entry = snapshot["run"]["id"], snapshot["windows"][0]["entry_node_ids"][0]
    reading = runner.add_reading(run_id, "读轨")["readings"][0]["id"]
    scope = f"reading:{reading}"

    class PagedReading(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["work_seq"] == 0:
                decision = {"action": "navigate", "args": {"node_id": entry, **range_args}}
            else:
                decision = {"action": "render", "args": {"visit_ids": [frame["visits"][-1]["id"]]}}
            return ModelResult(validate_decision(frame, decision))

        async def render(self, frame, *, repair=None):
            # Builds only a payload; no transport or paid provider method is called.
            projection = DeepSeekGrowthProvider(Settings()).projection(frame, "render")
            projected = json.loads(projection["messages"][1]["content"])
            start, end = expected_range
            assert projected["sources"][0]["text"] == text[start:end]
            assert len(projected["sources"][0]["text"]) == end - start
            return await super().render(frame, repair=repair)

    monkeypatch.setattr(runner, "provider", lambda state: PagedReading())
    await runner.step_scope(run_id, scope)
    await runner.step_scope(run_id, scope)
    state = runner.load(run_id)
    visit = runner.rows(state, "visit", scope)[0]
    passage = runner.rows(state, "passage", scope)[0]
    span = visit["source_spans"][0]
    assert (span["start"], span["end"]) == expected_range
    assert passage["source_spans"] == visit["source_spans"]
    assert state.config["usage"]["calls"] == 4  # navigate, decide, actual method invocation, render


async def test_dense_graph_keeps_latest_edges_and_can_query_then_traverse_older_edges(runner, monkeypatch):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    entry, target = [window["entry_node_ids"][0] for window in snapshot["windows"]]
    state = runner.load(run_id)
    for index in range(105):
        state = add_link(runner.store, state, entry, target, scope_id="window:door", request_id=f"edge-{index}")
    links = runner.rows(state, "link", owner=True, limit=200)
    reading = runner.add_reading(run_id, "读轨")["readings"][0]["id"]
    scope = f"reading:{reading}"
    state = navigate(runner.store, runner.load(run_id), reading, entry, transition="entry", request_id="initial")
    frame = runner.frame(state, scope, runner.session(state, scope))
    assert links[-1]["id"] in {link["id"] for link in frame["links"]}
    assert links[0]["id"] not in {link["id"] for link in frame["links"]}
    commit = state.graph_commit

    class QueryThenTraverse(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["work_seq"] == 0:
                decision = {"action": "query", "args": {"kind": "link", "query": "*", "offset": 0}}
            else:
                discovered = frame["pages"][0]["items"][0]
                assert discovered["id"] == links[0]["id"]
                decision = {"action": "navigate", "args": {"node_id": target, "edge_id": discovered["id"],
                                                            "transition": "edge"}}
            return ModelResult(validate_decision(frame, decision))

    monkeypatch.setattr(runner, "provider", lambda state: QueryThenTraverse())
    await runner.step_scope(run_id, scope)
    await runner.step_scope(run_id, scope)
    state = runner.load(run_id)
    assert state.reading_tracks[reading].cursor_node_id == target
    assert state.graph_commit == commit
    assert runner.rows(state, "visit", scope)[-1]["via_link_id"] == links[0]["id"]


@pytest.mark.parametrize("parallel", [1, 2, None])
async def test_continuous_windows_share_parallel_cap_and_pause_settles_calls(runner, monkeypatch, parallel):
    windows = [{"id": key, "seed": "她站在门前。"} for key in ("a", "b", "c")]
    snapshot = runner.create(title="连续运行", provider="fixture", windows=windows,
                            limits=Limits(max_calls=None, max_tokens=None, max_windows=None, max_parallel=parallel))
    run_id = snapshot["run"]["id"]
    expected = parallel or len(windows)
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedFixture(FixtureGrowthProvider):
        active = peak = calls = 0

        async def decide(self, frame, *, repair=None):
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            if self.active == expected:
                entered.set()
            try:
                await release.wait()
                return ModelResult({"action": "wait", "args": {}})
            finally:
                self.active -= 1

    provider = GatedFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    try:
        await runner.start(run_id)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert provider.peak == provider.calls == expected
        assert runner.load(run_id).config["usage"]["calls"] == expected
        assert runner.load(run_id).config["usage"]["reserved_tokens"] > 0
        paused = await runner.pause(run_id)
        assert not paused["active_scopes"] and not runner.busy() and provider.active == 0
        usage = runner.load(run_id).config["usage"]
        assert usage["calls"] == usage["failed_calls"] == expected
        assert usage["reserved_tokens"] == 0 and usage["unknown_tokens"] > 0
        assert not runner.rows(runner.load(run_id), "receipt", owner=True)
    finally:
        await runner.shutdown()


async def test_continuous_window_can_advance_while_another_window_is_still_calling(runner, monkeypatch):
    snapshot = runner.create(title="独立连续运行", provider="fixture", windows=WINDOWS,
                            limits=Limits(max_calls=None, max_tokens=None, max_windows=None, max_parallel=None))
    run_id = snapshot["run"]["id"]
    first_calls, release_door, second_door = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never_release = asyncio.Event()

    class IndependentFixture(FixtureGrowthProvider):
        started = set()

        async def decide(self, frame, *, repair=None):
            self.started.add(frame["scope_id"])
            if len(self.started) == 2:
                first_calls.set()
            if frame["scope_id"] == "window:door" and frame["work_seq"] == 0:
                await release_door.wait()
                return ModelResult({"action": "inspect", "args": {"method_id": frame["method"]["id"]}})
            if frame["scope_id"] == "window:door":
                second_door.set()
            await never_release.wait()
            return ModelResult({"action": "wait", "args": {}})

    monkeypatch.setattr(runner, "provider", lambda state: IndependentFixture())
    try:
        await runner.start(run_id)
        await asyncio.wait_for(first_calls.wait(), timeout=2)
        release_door.set()
        await asyncio.wait_for(second_door.wait(), timeout=2)
        state = runner.load(run_id)
        assert runner.session(state, "window:door")["work_seq"] == 1
        assert runner.session(state, "window:stairs")["work_seq"] == 0
        assert state.windows["stairs"].status == "calling"
        await runner.pause(run_id)
        assert not runner.busy() and runner.load(run_id).config["usage"]["reserved_tokens"] == 0
    finally:
        await runner.shutdown()


async def test_start_during_manual_step_rejects_busy_or_retains_that_window_worker(runner, monkeypatch):
    snapshot = runner.create(title="手动与连续衔接", provider="fixture", windows=WINDOWS,
                            limits=Limits(max_calls=None, max_tokens=None, max_windows=None, max_parallel=None))
    run_id = snapshot["run"]["id"]
    manual_entered, other_entered, continuous_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_manual, never_release = asyncio.Event(), asyncio.Event()

    class OverlappingFixture(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["scope_id"] == "window:door" and frame["work_seq"] == 0:
                manual_entered.set()
                await release_manual.wait()
                return ModelResult({"action": "inspect", "args": {"method_id": frame["method"]["id"]}})
            if frame["scope_id"] == "window:door":
                continuous_entered.set()
            else:
                other_entered.set()
            await never_release.wait()
            return ModelResult({"action": "wait", "args": {}})

    monkeypatch.setattr(runner, "provider", lambda state: OverlappingFixture())
    manual = asyncio.create_task(runner.step_scope(run_id, "window:door"))
    try:
        await asyncio.wait_for(manual_entered.wait(), timeout=2)
        try:
            await runner.start(run_id)
        except KernelError as error:
            assert error.code == "RUN_BUSY"
            assert not runner.tasks
            return
        await asyncio.wait_for(other_entered.wait(), timeout=2)
        release_manual.set()
        await asyncio.wait_for(manual, timeout=2)
        await asyncio.wait_for(continuous_entered.wait(), timeout=2)
        assert runner.session(runner.load(run_id), "window:door")["work_seq"] >= 1
    finally:
        manual.cancel()
        await asyncio.gather(manual, return_exceptions=True)
        await runner.shutdown()
