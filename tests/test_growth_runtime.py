"""Offline vertical checks for graph work, actual model inputs and recovery."""

from __future__ import annotations

import json

import pytest
from opentale.config import Settings
from opentale.domain import Limits
from opentale.growth_kernel import add_node, navigate
from opentale.growth_runner import GrowthRunner, identifier
from opentale.growth_types import ObjectRef
from opentale.kernel import KernelError
from opentale.providers.base import ModelResult, ProviderError
from opentale.providers.feedback import OutputFeedback
from opentale.providers.growth import FixtureGrowthProvider, validate_decision

KEY = "offline-runtime-dummy-key"
WINDOWS = [
    {"id": "door", "label": "门前", "seed": "她把手放到门闩上，看见🕯️照着台阶。", "direction": "继续门前的生活"},
    {"id": "stairs", "label": "台阶", "seed": "他在台阶下等着，袖口沾了一滴雨。", "direction": "接住台阶下的回应"},
]


@pytest.fixture
def runner(tmp_path):
    runtime = GrowthRunner(tmp_path, Settings(api_key=KEY))
    yield runtime
    runtime.store.close()


def create(runner, *, limits=None):
    return runner.create(title="离线生长与阅读", provider="fixture", windows=WINDOWS,
                         shared_seed="门前有一杯温水。", limits=limits or Limits(max_calls=None, max_tokens=None))


async def advance(runner, run_id, scope, count):
    snapshot = runner.snapshot(run_id)
    for _ in range(count):
        snapshot = await runner.step_scope(run_id, scope)
    return snapshot


def own_rows(runner, run_id, kind, scope):
    return [row for row in runner.rows(runner.load(run_id), kind, scope) if row.get("scope_id") == scope]


async def test_actual_trial_feedback_revision_adoption_new_trial_and_publishing(runner):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    initial_commit = snapshot["graph_commit"]
    initial_nodes = len(snapshot["nodes"])
    snapshot = await advance(runner, run_id, "window:door", 20)
    receipts = own_rows(runner, run_id, "receipt", "window:door")
    assert [row["action"] for row in receipts[:9]] == [
        "inspect", "apply", "try_write", "respond", "revise", "adopt", "apply", "try_write", "publish",
    ]
    assert snapshot["graph_commit"] == initial_commit + 4
    assert len(snapshot["nodes"]) == initial_nodes + 4
    assert snapshot["windows"][1]["revision"] == 1
    trials = own_rows(runner, run_id, "trial", "window:door")
    methods = own_rows(runner, run_id, "method", "window:door")
    responses = own_rows(runner, run_id, "response", "window:door")
    assert len(methods) == 2 and len(responses) == 1
    original, revised = methods
    assert revised["parent_ref"] == {"kind": "method", "object_id": original["id"], "version": original["version"]}
    assert {"kind": "response", "object_id": responses[0]["id"], "version": responses[0]["version"]} in revised["source_refs"]
    assert responses[0]["about_ids"] == [trials[0]["id"]]
    assert trials[0]["method_ref"]["version"] == original["version"]
    assert trials[1]["method_ref"] == {"kind": "method", "object_id": revised["id"], "version": revised["version"]}
    assert not trials[0]["adopted"] and trials[1]["adopted"]
    first = runner.expand(runner.load(run_id), "trial", trials[0])["text"]
    second = runner.expand(runner.load(run_id), "trial", trials[1])["text"]
    assert first != second and "各自说出要去的地方" in second
    # The successful new trial actually received the new method and original feedback.
    requests = runner.rows(runner.load(run_id), "request", owner=True)
    new_draft = next(row for row in requests if row["stage"] == "draft" and row["frame"]["method"]["id"] == revised["id"])
    assert new_draft["frame"]["method"]["version"] == revised["version"]
    assert new_draft["projection"]["frame"]["responses"][0]["text"] == runner.body(responses[0])
    assert snapshot["run"]["usage"]["reserved_tokens"] == 0


async def test_multiple_actual_visits_and_unicode_source_passages_on_unchanged_graph(runner):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    snapshot = await advance(runner, run_id, "window:door", 20)
    commit = snapshot["graph_commit"]
    door_entry = snapshot["windows"][0]["entry_node_ids"][0]
    snapshot = runner.add_reading(run_id, "门前读轨", [door_entry])
    track_id = snapshot["readings"][0]["id"]
    snapshot = await advance(runner, run_id, f"reading:{track_id}", 20)
    track = snapshot["readings"][0]
    assert snapshot["graph_commit"] == commit
    assert len(track["visits"]) >= 4 and len(track["passages"]) >= 4
    assert track["visits"][0]["node_id"] == door_entry
    assert all(visit["observed_graph_commit"] == commit for visit in track["visits"])
    assert len({visit["read_seq"] for visit in track["visits"]}) == len(track["visits"])
    visits = {visit["id"]: visit for visit in track["visits"]}
    for passage in track["passages"]:
        assert all(visit_id in visits for visit_id in passage["visit_ids"])
        for span in passage["source_spans"]:
            node = runner.store.get(run_id, "node", span["node_id"], version=span["node_version"], include_private=True)
            assert node["text_ref"] == span["text_ref"]
            source = runner.body(node)
            assert (span["start"], span["end"]) == (0, len(source))
            assert source[span["start"]:span["end"]] in passage["text"]
    # Source offsets count Unicode characters, including the emoji and combining selector.
    first_span = track["passages"][0]["source_spans"][0]
    assert first_span["end"] == len(WINDOWS[0]["seed"])
    assert first_span["end"] < len(WINDOWS[0]["seed"].encode("utf-16-le")) // 2


async def test_two_readers_preserve_entries_independent_sequences_and_pending_work(runner):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    await advance(runner, run_id, "window:door", 9)
    snapshot = await advance(runner, run_id, "window:stairs", 9)
    commit = snapshot["graph_commit"]
    entries = [window["entry_node_ids"][0] for window in snapshot["windows"]]
    snapshot = runner.add_reading(run_id, "甲读轨", [entries[0]])
    first_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "甲读轨")
    snapshot = runner.add_reading(run_id, "乙读轨", [entries[1]])
    second_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "乙读轨")
    await advance(runner, run_id, f"reading:{first_id}", 3)
    first_before = runner.load(run_id).reading_tracks[first_id].model_dump()
    snapshot = await advance(runner, run_id, f"reading:{second_id}", 6)
    assert runner.load(run_id).reading_tracks[first_id].model_dump() == first_before
    first = next(track for track in snapshot["readings"] if track["id"] == first_id)
    second = next(track for track in snapshot["readings"] if track["id"] == second_id)
    assert first["visits"][0]["node_id"] == entries[0]
    assert second["visits"][0]["node_id"] == entries[1]
    assert first["read_seq"] < second["read_seq"]
    assert not {v["id"] for v in first["visits"]}.intersection(v["id"] for v in second["visits"])
    assert snapshot["graph_commit"] == commit
    assert runner.session(runner.load(run_id), f"reading:{first_id}")["work_seq"] == 3
    assert runner.session(runner.load(run_id), f"reading:{second_id}")["work_seq"] == 6


class SpyFixture(FixtureGrowthProvider):
    def __init__(self):
        self.calls = []

    async def decide(self, frame, *, repair=None):
        self.calls.append(("decide", frame["scope_id"], frame["work_seq"]))
        return await super().decide(frame, repair=repair)

    async def draft(self, frame, *, repair=None):
        self.calls.append(("draft", frame["scope_id"], frame["work_seq"]))
        return await super().draft(frame, repair=repair)


async def test_accepted_request_replay_does_not_call_provider_or_reserve_budget_again(runner, monkeypatch):
    snapshot = create(runner)
    run_id, scope = snapshot["run"]["id"], "window:door"
    spy = SpyFixture()
    monkeypatch.setattr(runner, "provider", lambda state: spy)
    current = runner.load(run_id)
    frozen = runner.frame(current, scope, runner.session(current, scope))
    value, request_id = await runner.call(run_id, scope, 1, "decide", frozen)
    usage = runner.load(run_id).config["usage"].copy()
    # A restart can replay the accepted immutable request despite a later local frame.
    newer = {**frozen, "direction": "后来变化的方向", "work_seq": 9}
    assert await runner.call(run_id, scope, 1, "decide", newer) == (value, request_id)
    assert spy.calls == [("decide", scope, 0)]
    assert runner.load(run_id).config["usage"] == usage
    saved = runner.store.get(run_id, "request", request_id, include_private=True)
    assert saved["frame"] == frozen and saved["status"] == "accepted"


async def test_published_result_recovers_after_installation_without_duplicate_node_or_call(runner, monkeypatch):
    snapshot = create(runner)
    run_id, scope = snapshot["run"]["id"], "window:door"
    spy = SpyFixture()
    monkeypatch.setattr(runner, "provider", lambda state: spy)
    await advance(runner, run_id, scope, 8)
    execute = runner.execute
    interrupted = False

    async def interrupt_after_publish(*args, **kwargs):
        nonlocal interrupted
        result = await execute(*args, **kwargs)
        if args[3]["action"] == "publish" and not interrupted:
            interrupted = True
            raise KernelError("TEST_INTERRUPTED", "已发布结果等待工作收据恢复。")
        return result

    monkeypatch.setattr(runner, "execute", interrupt_after_publish)
    with pytest.raises(KernelError, match="工作收据恢复"):
        await runner.step_scope(run_id, scope)
    installed = runner.snapshot(run_id)
    calls = list(spy.calls)
    usage = installed["run"]["usage"].copy()
    recovered = await runner.step_scope(run_id, scope)
    assert recovered["graph_commit"] == installed["graph_commit"]
    assert len(recovered["nodes"]) == len(installed["nodes"])
    assert spy.calls == calls
    assert recovered["run"]["usage"] == usage
    receipts = own_rows(runner, run_id, "receipt", scope)
    assert [row["action"] for row in receipts].count("publish") == 1
    assert runner.session(runner.load(run_id), scope)["work_seq"] == 9


async def test_accepted_draft_keeps_original_method_and_source_refs_after_later_adoption(runner, monkeypatch):
    snapshot = create(runner)
    run_id, scope = snapshot["run"]["id"], "window:door"
    spy = SpyFixture()
    monkeypatch.setattr(runner, "provider", lambda state: spy)
    await advance(runner, run_id, scope, 2)
    before = runner.load(run_id)
    original_frame = runner.frame(before, scope, runner.session(before, scope))
    original_method = original_frame["method"]
    original_ref = {"kind": "method", "object_id": original_method["id"], "version": original_method["version"]}
    put = runner.put

    def interrupt_candidate(rid, kind, value, **kwargs):
        if kind == "candidate":
            raise KernelError("TEST_INTERRUPTED", "已接受试写等待安装。")
        return put(rid, kind, value, **kwargs)

    monkeypatch.setattr(runner, "put", interrupt_candidate)
    with pytest.raises(KernelError, match="等待安装"):
        await runner.step_scope(run_id, scope)
    monkeypatch.setattr(runner, "put", put)
    calls = list(spy.calls)
    # Another explicit adoption after interruption must affect subsequent work,
    # while the already accepted draft retains the version that produced it.
    state = runner.load(run_id)
    revised = put(run_id, "method", {**original_method, "id": "method-later-adoption",
                                     "instruction": "采用另一种新的组织方式。", "parent_ref": original_ref})
    later_ref = {"kind": "method", "object_id": revised["id"], "version": revised["version"]}
    state.windows["door"].method_ref = ObjectRef.model_validate(later_ref)
    runner.store.save_head(state)
    session = runner.session(state, scope)
    session["applied_method"] = later_ref
    session["selected_node_ids"] = [state.windows["stairs"].entry_node_ids[0]]
    put(run_id, "session", session)
    await runner.step_scope(run_id, scope)
    trial = own_rows(runner, run_id, "trial", scope)[0]
    assert trial["method_ref"] == original_ref
    assert trial["source_refs"] == [
        {"kind": "node", "object_id": source["id"], "version": source["version"]}
        for source in original_frame["sources"]
    ]
    candidate = runner.store.get(run_id, "candidate", trial["result_ref"]["object_id"],
                                 version=trial["result_ref"]["version"], include_private=True)
    assert candidate["method_ref"] == original_ref
    assert runner.load(run_id).windows["door"].method_ref.model_dump() == later_ref
    assert spy.calls == calls


async def test_budget_exhaustion_preserves_cursor_and_other_scope_can_resume_when_budget_extended(runner):
    snapshot = create(runner, limits=Limits(max_calls=2, max_tokens=None))
    run_id, scope = snapshot["run"]["id"], "window:door"
    await advance(runner, run_id, scope, 2)
    before = runner.load(run_id)
    with pytest.raises(KernelError) as captured:
        await runner.step_scope(run_id, scope)
    assert captured.value.code == "BUDGET_EXHAUSTED"
    failed = runner.load(run_id)
    assert failed.graph_commit == before.graph_commit
    assert failed.config["usage"]["calls"] == 2
    assert failed.config["usage"]["reserved_tokens"] == 0
    assert runner.session(failed, scope)["work_seq"] == 2
    failed.config["limits"]["max_calls"] = None
    runner.store.save_head(failed)
    snapshot = await runner.step_scope(run_id, "window:stairs")
    assert own_rows(runner, run_id, "receipt", "window:stairs")[-1]["action"] == "inspect"
    snapshot = await runner.step_scope(run_id, scope)
    assert own_rows(runner, run_id, "receipt", scope)[-1]["action"] == "try_write"
    assert snapshot["run"]["usage"]["calls"] == 5


async def test_token_preflight_never_calls_or_leaves_reservation(runner, monkeypatch):
    snapshot = create(runner, limits=Limits(max_calls=None, max_tokens=1))
    run_id = snapshot["run"]["id"]
    spy = SpyFixture()
    monkeypatch.setattr(runner, "provider", lambda state: spy)
    with pytest.raises(KernelError) as captured:
        await runner.step_scope(run_id, "window:door")
    assert captured.value.code == "BUDGET_EXHAUSTED"
    assert not spy.calls
    usage = runner.load(run_id).config["usage"]
    assert usage["calls"] == usage["reserved_tokens"] == usage["unknown_tokens"] == 0


async def test_one_reader_provider_failure_does_not_block_another_scope(runner, monkeypatch):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    snapshot = runner.add_reading(run_id, "失败读轨")
    failing_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "失败读轨")
    snapshot = runner.add_reading(run_id, "继续读轨")
    good_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "继续读轨")

    class IsolatedFailure(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["scope_id"] == f"reading:{failing_id}":
                raise ProviderError("PROVIDER_UNAVAILABLE", "服务暂时不可用。", True)
            return await super().decide(frame, repair=repair)

    monkeypatch.setattr(runner, "provider", lambda state: IsolatedFailure())
    with pytest.raises(ProviderError):
        await runner.step_scope(run_id, f"reading:{failing_id}")
    await advance(runner, run_id, f"reading:{good_id}", 3)
    state = runner.load(run_id)
    assert state.reading_tracks[failing_id].read_seq == 0
    assert state.reading_tracks[failing_id].status == "error"
    assert state.reading_tracks[good_id].read_seq == 2
    assert state.config["usage"]["reserved_tokens"] == 0
    assert state.config["usage"]["failed_calls"] == 1


@pytest.mark.parametrize("field", ["seed", "direction", "shared_seed"])
def test_secret_bearing_creation_material_is_rejected_without_persistence(runner, field):
    windows = [dict(window) for window in WINDOWS]
    shared = "共同入口。"
    if field == "shared_seed":
        shared = KEY
    else:
        windows[0][field] = KEY
    with pytest.raises(KernelError) as captured:
        runner.create(title="离线", provider="fixture", windows=windows, shared_seed=shared)
    assert captured.value.code == "CONTENT_UNSAFE"
    assert KEY not in str(captured.value)
    for summary in runner.store.list_runs():
        saved = runner.snapshot(summary["run_id"])
        assert KEY not in json.dumps(saved, ensure_ascii=False)


async def test_settings_and_hidden_transport_metadata_never_enter_saved_requests(runner):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]
    await advance(runner, run_id, "window:door", 9)
    state = runner.load(run_id)
    data = {
        "head": state.model_dump(), "snapshot": runner.snapshot(run_id),
        "requests": runner.rows(state, "request", owner=True),
    }
    serialized = json.dumps(data, ensure_ascii=False)
    assert KEY not in serialized and "Authorization" not in serialized and "reasoning_content" not in serialized
    assert "api.deepseek.com" not in serialized
    assert state.config["usage"]["calls"] == 11
    assert state.config["usage"]["input_tokens"] == state.config["usage"]["output_tokens"] == 0


async def test_rejected_billed_output_usage_is_exact_and_error_body_stays_safe(runner, monkeypatch):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]

    class Rejected(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            raise ProviderError("MODEL_OUTPUT_INVALID", "工作动作无效。", True, usage=(70, 20))

    monkeypatch.setattr(runner, "provider", lambda state: Rejected())
    with pytest.raises(ProviderError):
        await runner.step_scope(run_id, "window:door")
    usage = runner.load(run_id).config["usage"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (70, 20)
    assert usage["failed_calls"] == 1 and usage["unknown_tokens"] == usage["reserved_tokens"] == 0
    request = runner.store.get(run_id, "request", f"request-{identifier('window:door', 1, 'decide')}", include_private=True)
    assert request["error_code"] == "MODEL_OUTPUT_INVALID"
    assert "error_body" not in request and "result" not in request


async def test_format_repair_uses_safe_feedback_frozen_context_and_counts_each_attempt(runner, monkeypatch):
    snapshot = create(runner)
    run_id = snapshot["run"]["id"]

    class Repairable(FixtureGrowthProvider):
        def __init__(self):
            self.repairs = []
            self.inputs = []

        async def decide(self, frame, *, repair=None):
            self.repairs.append(repair)
            self.inputs.append(json.dumps(frame, ensure_ascii=False, sort_keys=True))
            if repair is None:
                raise ProviderError("MODEL_OUTPUT_INVALID", "输出结构无效。", True, usage=(80, 20),
                                    feedback=OutputFeedback("SCHEMA_VALIDATION", ("SCHEMA_INVALID",)))
            decision = await super().decide(frame, repair=repair)
            return ModelResult(decision.value, 60, 15)

    provider = Repairable()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    snapshot = await runner.step_scope(run_id, "window:door")
    assert provider.repairs == [None, OutputFeedback("SCHEMA_VALIDATION", ("SCHEMA_INVALID",))]
    assert provider.inputs[0] == provider.inputs[1]
    usage = snapshot["run"]["usage"]
    assert usage["calls"] == 2 and usage["failed_calls"] == 1
    assert (usage["input_tokens"], usage["output_tokens"]) == (140, 35)
    assert usage["reserved_tokens"] == usage["unknown_tokens"] == 0
    assert own_rows(runner, run_id, "receipt", "window:door")[-1]["action"] == "inspect"


async def test_repair_attempt_cannot_exceed_call_budget_or_advance_scope(runner, monkeypatch):
    snapshot = create(runner, limits=Limits(max_calls=1, max_tokens=None))
    run_id = snapshot["run"]["id"]

    class Repairable(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if repair is None:
                raise ProviderError("MODEL_OUTPUT_INVALID", "输出结构无效。", True, usage=(80, 20),
                                    feedback=OutputFeedback("SCHEMA_VALIDATION", ("SCHEMA_INVALID",)))
            return await super().decide(frame, repair=repair)

    monkeypatch.setattr(runner, "provider", lambda state: Repairable())
    with pytest.raises(KernelError) as captured:
        await runner.step_scope(run_id, "window:door")
    assert captured.value.code == "BUDGET_EXHAUSTED"
    state = runner.load(run_id)
    assert state.config["usage"]["calls"] == 1
    assert state.config["usage"]["failed_calls"] == 1
    assert state.config["usage"]["reserved_tokens"] == 0
    assert runner.session(state, "window:door")["work_seq"] == 0
    assert not own_rows(runner, run_id, "receipt", "window:door")


async def test_evaluation_method_revision_changes_executable_structure_and_exact_feedback_refs(runner, monkeypatch):
    snapshot = create(runner)
    run_id, scope = snapshot["run"]["id"], "window:door"
    snapshot = await advance(runner, run_id, scope, 3)
    trial_id = own_rows(runner, run_id, "trial", scope)[0]["id"]
    door, stairs = [window["entry_node_ids"][0] for window in snapshot["windows"]]
    criterion = runner.put(run_id, "method", {
        "id": "criterion-old", "scope_id": scope, "instruction": "查看每个方向留下的具体动作。",
        "language_version": 3, "program": {"node_ids": [door]}, "private_scope": scope,
        "created_commit": snapshot["graph_commit"],
    })
    criterion_ref = {"kind": "method", "object_id": criterion["id"], "version": criterion["version"]}
    program = {"node_ids": [stairs], "groups": {"实际回应": [stairs]},
               "relations": [{"source_id": door, "target_id": stairs, "relation": "比较两处后果"}]}

    class CriteriaFixture(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            sequence = frame["work_seq"]
            if sequence == 3:
                decision = {"action": "respond", "args": {"text": "试写带来了动作，下一次查看两处回应之间的关系。",
                            "about_ids": [trial_id], "evaluation_method_id": criterion["id"]}}
            elif sequence == 4:
                response = next(row for row in reversed(frame["receipts"]) if row["action"] == "respond")
                decision = {"action": "revise", "args": {"method_id": criterion["id"],
                            "instruction": "把两处人物回应组织为同一关系，评议它们留下的各自后果。",
                            "input_ids": [response["details"]["response_id"]], "program": program,
                            "purpose": "改变评议所采用的比较依据"}}
            elif sequence == 5:
                revision = next(row for row in reversed(frame["receipts"]) if row["action"] == "revise")
                decision = {"action": "apply", "args": {"method_id": revision["details"]["method_id"]}}
            else:
                decision = {"action": "respond", "args": {"text": "新的评议比较两处回应，保留了它们不同的方向。",
                            "about_ids": [trial_id], "evaluation_method_id": frame["method"]["id"]}}
            return ModelResult(validate_decision(frame, decision))

    monkeypatch.setattr(runner, "provider", lambda state: CriteriaFixture())
    await advance(runner, run_id, scope, 4)
    responses = own_rows(runner, run_id, "response", scope)
    revised = next(method for method in own_rows(runner, run_id, "method", scope)
                   if method.get("parent_ref") == criterion_ref)
    revised_ref = {"kind": "method", "object_id": revised["id"], "version": revised["version"]}
    assert revised["program"]["node_ids"] == [stairs]
    assert revised["program"]["groups"] == program["groups"]
    assert revised["program"]["relations"] == program["relations"]
    assert responses[0]["evaluation_method_ref"] == criterion_ref
    assert responses[1]["evaluation_method_ref"] == revised_ref
    trial = own_rows(runner, run_id, "trial", scope)[0]
    assert trial["feedback_refs"] == [{"kind": "response", "object_id": row["id"], "version": row["version"]}
                                     for row in responses]
    session = runner.session(runner.load(run_id), scope)
    assert session["view"]["groups"]["实际回应"][0]["object_id"] == stairs
    assert session["view"]["relations"][0]["target_ref"]["object_id"] == stairs
    applied_request = next(request for request in runner.rows(runner.load(run_id), "request", owner=True)
                           if request["stage"] == "decide" and request["frame"]["work_seq"] == 6)
    assert applied_request["frame"]["method"]["version"] == revised["version"]


async def test_partial_unicode_visit_is_the_actual_render_request_source(runner, monkeypatch):
    snapshot = runner.create(title="原文区间", provider="fixture", windows=[
        {"id": "door", "label": "门前", "seed": "甲😀乙。", "direction": "查看门前的动作"},
    ], limits=Limits(max_calls=None, max_tokens=None))
    run_id = snapshot["run"]["id"]
    entry = snapshot["windows"][0]["entry_node_ids"][0]
    snapshot = runner.add_reading(run_id, "区间读轨", [entry])
    track_id = snapshot["readings"][0]["id"]

    class RangeFixture(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["work_seq"] == 1:
                decision = {"action": "navigate", "args": {"node_id": entry, "transition": "entry", "start": 1, "end": 2}}
                return ModelResult(validate_decision(frame, decision))
            return await super().decide(frame, repair=repair)

    monkeypatch.setattr(runner, "provider", lambda state: RangeFixture())
    snapshot = await advance(runner, run_id, f"reading:{track_id}", 3)
    track = snapshot["readings"][0]
    visit, passage = track["visits"][0], track["passages"][0]
    assert (visit["source_spans"][0]["start"], visit["source_spans"][0]["end"]) == (1, 2)
    assert passage["text"] == "😀"
    request = next(row for row in runner.rows(runner.load(run_id), "request", owner=True) if row["stage"] == "render")
    source = request["projection"]["frame"]["sources"][0]
    assert (source["text"], source["start"], source["end"]) == ("😀", 1, 2)
    assert request["projection"]["frame"]["visit_ids"] == [visit["id"]]
    assert request["projection"]["frame"]["source_spans"] == visit["source_spans"]


async def test_older_edge_discovered_by_query_page_changes_actual_navigation(runner, monkeypatch):
    snapshot = runner.create(title="分页发现关联", provider="fixture", windows=[
        {"id": "door", "label": "门前", "seed": "她站在共同入口。"},
    ], limits=Limits(max_calls=None, max_tokens=None))
    run_id, entry = snapshot["run"]["id"], snapshot["windows"][0]["entry_node_ids"][0]
    state = runner.load(run_id)
    target = None
    for index in range(45):
        state = add_node(runner.store, state, "door", f"第{index}条路的灯亮着。", parents=[entry], request_id=f"older-edge-{index}")
        if index == 0:
            target = state.windows["door"].working_node_id
    edge = next(row for row in runner.rows(state, "link", owner=True) if row["target_id"] == target)
    snapshot = runner.add_reading(run_id, "寻找旧边", [entry])
    track_id = snapshot["readings"][0]["id"]
    state = navigate(runner.store, runner.load(run_id), track_id, entry,
                     observed_graph_commit=state.graph_commit, request_id="known-entry")
    initial = runner.frame(state, f"reading:{track_id}", runner.session(state, f"reading:{track_id}"))
    assert edge["id"] not in {row["id"] for row in initial["links"]}
    assert target not in {row["id"] for row in initial["nodes"]}

    class QueryFixture(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["work_seq"] == 0:
                decision = {"action": "query", "args": {"kind": "link", "query": "*", "offset": 0}}
            elif frame["work_seq"] == 1:
                discovered = next(row for page in frame["pages"] for row in page["items"] if row["id"] == edge["id"])
                assert discovered["target_id"] == target
                decision = {"action": "navigate", "args": {"node_id": target, "edge_id": discovered["id"],
                            "transition": "edge", "start": 0, "end": 5}}
            else:
                return await super().decide(frame, repair=repair)
            return ModelResult(validate_decision(frame, decision))

    monkeypatch.setattr(runner, "provider", lambda state: QueryFixture())
    snapshot = await advance(runner, run_id, f"reading:{track_id}", 3)
    track = snapshot["readings"][0]
    assert track["cursor_node_id"] == target
    visit = track["visits"][-1]
    assert visit["node_id"] == target and visit["via_link_id"] == edge["id"]
    assert (visit["source_spans"][0]["start"], visit["source_spans"][0]["end"]) == (0, 5)
    assert track["passages"][-1]["text"] == "第0条路的"
