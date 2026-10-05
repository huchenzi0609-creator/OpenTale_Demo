"""Parallel reading continuations share a world, never another reading's history.

All evaluators are deterministic local fixtures with dummy Settings. The barriers
below verify real coroutine overlap rather than inferring concurrency from timing.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import opentale.runner as runner_module
import pytest
from fastapi.testclient import TestClient
from opentale.api import create_app
from opentale.capacity import MAX_TOKEN_BUDGET, budget_bounds
from opentale.domain import Limits, Presentation, Snapshot
from opentale.kernel import KernelError, frame_for, install_presentation
from opentale.providers import FixtureProvider, ModelResult, ProviderError
from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store
from pydantic import ValidationError


def track(state: Snapshot, identifier: str = "main"):
    return next(item for item in state.reading_tracks if item.id == identifier)


def new_track_id(before: Snapshot, after: Snapshot) -> str:
    existing = {item.id for item in before.reading_tracks}
    return next(item.id for item in after.reading_tracks if item.id not in existing)


def runtime(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(max_calls=100, max_tokens=5120000, max_parallel=3, max_windows=8)
    store.save(snapshot)
    return store, Runner(store, settings), snapshot.run.id


class ReadingEvaluator:
    """A two-reader barrier, optional one-shot failure, and exact call ledger."""

    def __init__(self, *, barrier=False, fail_start=None):
        self.fixture = FixtureProvider()
        self.barrier = barrier
        self.fail_start = fail_start
        self.failed = False
        self.calls = Counter()
        self.frames = []
        self.presenters_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active_presenters = 0
        self.max_active_presenters = 0

    async def propose(self, frame):
        self.calls[("propose", frame.position_id)] += 1
        return await self.fixture.propose(frame)

    async def realize(self, frame):
        self.calls[("realize", frame.position_id)] += 1
        return await self.fixture.realize(frame)

    async def present(self, frame):
        key = frame.start_position_id or "main"
        self.calls[("present", key)] += 1
        self.frames.append(frame.model_copy(deep=True))
        self.active_presenters += 1
        self.max_active_presenters = max(self.max_active_presenters, self.active_presenters)
        if self.active_presenters >= 2:
            self.presenters_entered.set()
        try:
            if self.barrier:
                await self.release.wait()
            if frame.start_position_id == self.fail_start and self.fail_start and not self.failed:
                self.failed = True
                raise ProviderError("MOCK_READING_FAILURE", "测试读法暂时失败。", True, usage=(2, 1))
            return ModelResult((await self.fixture.present(frame)).value, 2, 1)
        finally:
            self.active_presenters -= 1


class StageFailureEvaluator(ReadingEvaluator):
    def __init__(self, phase):
        super().__init__()
        self.phase = phase

    def fail_once(self, phase, frame):
        revision = 1 if phase == "propose" else 2
        if phase == self.phase and frame.revision == revision and frame.position_id == "keeper" and not self.failed:
            self.failed = True
            self.calls[(phase, frame.position_id)] += 1
            raise ProviderError("MOCK_STAGE_FAILURE", "第二窗口的测试阶段暂时失败。", True, usage=(0, 0))

    async def propose(self, frame):
        self.fail_once("propose", frame)
        return await super().propose(frame)

    async def realize(self, frame):
        self.fail_once("realize", frame)
        return await super().realize(frame)


def test_total_token_budget_accepts_decimal_5_12m_and_rejects_excess():
    assert MAX_TOKEN_BUDGET == budget_bounds()["max_tokens"] == 5120000
    assert Limits(max_tokens=5120000).max_tokens == 5120000
    assert Limits().max_tokens == 128000
    with pytest.raises(ValidationError):
        Limits(max_tokens=5120001)


async def test_two_starts_share_world_but_keep_separate_reading_continuations(snapshot, settings, tmp_path):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    configured = runner.add_reading(run_id, "守门人的读法", "keeper")
    second_id = new_track_id(snapshot, configured)
    assert configured.run.usage.calls == 0 and not track(configured, second_id).story
    first = await runner.step(run_id)
    assert first.run.revision == 1 and first.run.usage.calls == 8
    assert len(first.story) == len(track(first, second_id).story) == 1
    assert track(first, second_id).story[0].focus_position_id == "keeper"
    assert track(first, second_id).story[0].id != first.story[0].id
    first_main = first.story[0].model_dump()
    first_other = track(first, second_id).story[0].model_dump()
    second = await runner.step(run_id)
    assert second.run.usage.calls == 16 and second.run.usage.windows == 2
    assert first_main == second.story[0].model_dump()
    assert first_other == track(second, second_id).story[0].model_dump()
    for reading in second.reading_tracks:
        own_ids = {segment.id for segment in reading.story}
        assert all(set(segment.reinterprets) <= own_ids for segment in reading.story)
        frame = runner.reading_frame(second, reading.id)
        assert frame.revision == second.run.revision
        assert {segment["id"] for segment in frame.previous_segments} <= own_ids
        assert all("source_position_id" not in use
                   for candidate in frame.candidates for use in candidate["active_uses"])
        assert all(position.seed not in frame.model_dump_json() for position in second.positions)
    assert verify_replay(second, store.read_events(run_id))["ok"]


async def test_readers_actually_overlap_and_runtime_edits_are_rejected(
    snapshot, settings, tmp_path, monkeypatch,
):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    added = runner.add_reading(run_id, "另一个入口", "keeper")
    second_id = new_track_id(snapshot, added)
    evaluator = ReadingEvaluator(barrier=True)
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: evaluator)
    task = asyncio.create_task(runner.step(run_id))
    try:
        await asyncio.wait_for(evaluator.presenters_entered.wait(), timeout=2)
        assert evaluator.max_active_presenters == 2
        for change in (
            lambda: runner.add_reading(run_id, "不可并发修改", "reader"),
            lambda: runner.set_reading_enabled(run_id, second_id, False),
        ):
            with pytest.raises(KernelError) as error:
                change()
            assert error.value.code == "RUN_BUSY"
        with pytest.raises(KernelError) as error:
            await runner.present_reading(run_id, second_id)
        assert error.value.code == "RUN_BUSY"
    finally:
        evaluator.release.set()
        result = await asyncio.wait_for(task, timeout=2)
    assert result.pending is None and result.run.usage.calls == 8
    assert len(track(store.load(run_id), second_id).story) == 1


async def test_partial_presentation_failure_retries_only_missing_reader(
    snapshot, settings, tmp_path, monkeypatch,
):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    added = runner.add_reading(run_id, "暂时中断的读法", "keeper")
    second_id = new_track_id(snapshot, added)
    evaluator = ReadingEvaluator(fail_start="keeper")
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: evaluator)
    with pytest.raises(ProviderError, match="测试读法暂时失败"):
        await runner.step(run_id)
    interrupted = store.load(run_id)
    assert interrupted.pending.committed and interrupted.run.revision == 1
    assert "main" in interrupted.pending.presentations and second_id not in interrupted.pending.presentations
    assert len(interrupted.story) == 1 and not track(interrupted, second_id).story
    assert interrupted.run.usage.windows == 0 and interrupted.run.usage.failed_calls == 1
    assert verify_replay(interrupted, store.read_events(run_id))["ok"]
    original_segment = interrupted.story[0].model_dump()
    store.recover()
    resumed = await Runner(store, settings).step(run_id)
    assert resumed.pending is None and resumed.run.revision == 1
    assert resumed.run.usage.windows == 1 and resumed.run.usage.calls == 9
    assert resumed.run.usage.input_tokens == 6 and resumed.run.usage.output_tokens == 3
    assert resumed.story[0].model_dump() == original_segment
    assert evaluator.calls[("present", "main")] == 1
    assert evaluator.calls[("present", "keeper")] == 2
    assert all(evaluator.calls[("propose", position.id)] == 1 for position in resumed.positions)
    assert all(evaluator.calls[("realize", position.id)] == 1 for position in resumed.positions)
    assert verify_replay(resumed, store.read_events(run_id))["ok"]


async def test_complete_pending_world_can_be_presented_and_resumed_without_extra_calls(
    snapshot, settings, tmp_path, monkeypatch,
):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    added = runner.add_reading(run_id, "等待恢复的读法", "keeper")
    reading_id = new_track_id(snapshot, added)
    evaluator = ReadingEvaluator(fail_start="keeper")
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: evaluator)
    with pytest.raises(ProviderError):
        await runner.step(run_id)
    failed = store.load(run_id)
    assert failed.pending.committed and len(failed.pending.realizations) == len(failed.positions)
    disabled = runner.set_reading_enabled(run_id, reading_id, False)
    assert disabled.pending == failed.pending and disabled.run.usage == failed.run.usage
    configured = runner.add_reading(run_id, "窗口暂停时新加的入口", "reader", enabled=False)
    assert configured.pending == failed.pending and configured.run.usage == failed.run.usage
    presented = await runner.present_reading(run_id, reading_id)
    assert reading_id in presented.pending.presentations
    assert presented.run.usage.calls == 9 and presented.run.usage.windows == 0
    runner.set_reading_enabled(run_id, reading_id, True)
    resumed = await runner.step(run_id)
    assert resumed.pending is None and resumed.run.usage.calls == 9
    assert resumed.run.usage.windows == 1 and len(resumed.story) == 1
    assert len(track(resumed, reading_id).story) == 1
    assert all(evaluator.calls[("propose", position.id)] == 1 for position in resumed.positions)
    assert all(evaluator.calls[("realize", position.id)] == 1 for position in resumed.positions)
    assert verify_replay(resumed, store.read_events(run_id))["ok"]


async def test_window_call_admission_includes_every_enabled_reader(snapshot, settings, tmp_path):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    runner.add_reading(run_id, "第二阅读的额度", "keeper")
    runner.update_limits(run_id, Limits(max_calls=7, max_tokens=5120000))
    with pytest.raises(KernelError) as error:
        await runner.step(run_id)
    assert error.value.code == "BUDGET_EXHAUSTED"
    exhausted = store.load(run_id)
    assert exhausted.run.usage.calls == 0 and exhausted.run.revision == 0


async def test_partial_new_revision_does_not_treat_previous_surfaces_as_current_world(
    snapshot, settings, tmp_path, monkeypatch,
):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    evaluator = StageFailureEvaluator("realize")
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: evaluator)
    first = await runner.step(run_id)
    with pytest.raises(ProviderError):
        await runner.step(run_id)
    interrupted = store.load(run_id)
    unfinished = next(item for item in interrupted.continuations if item.position_id == "keeper")
    assert unfinished.surface == next(item.surface for item in first.continuations if item.position_id == "keeper")
    assert unfinished.frame_revision == 2 and unfinished.status == "pending"
    before = interrupted.model_dump()
    with pytest.raises(KernelError) as error:
        await runner.present_reading(run_id, "main")
    assert error.value.code == "READING_NOT_READY"
    assert store.load(run_id).model_dump() == before
    resumed = await runner.step(run_id)
    assert resumed.run.revision == 2 and len(resumed.story) == 2
    assert verify_replay(resumed, store.read_events(run_id))["ok"]


async def test_drafting_next_window_keeps_completed_world_available_for_a_new_reading(
    snapshot, settings, tmp_path, monkeypatch,
):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    evaluator = StageFailureEvaluator("propose")
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: evaluator)
    await runner.step(run_id)
    with pytest.raises(ProviderError):
        await runner.step(run_id)
    interrupted = store.load(run_id)
    assert interrupted.run.revision == 1 and interrupted.pending.base_revision == 1
    assert not interrupted.pending.committed and len(interrupted.pending.proposals) == 2
    added = runner.add_reading(run_id, "暂停提案时读已完成的世界", "reader", enabled=False)
    reading_id = new_track_id(interrupted, added)
    presented = await runner.present_reading(run_id, reading_id)
    assert presented.run.revision == 1 and presented.run.usage.windows == 1
    assert presented.pending.proposals == interrupted.pending.proposals
    assert not presented.pending.committed
    assert track(presented, reading_id).story[0].revision == 1
    resumed = await runner.step(run_id)
    assert resumed.run.revision == 2 and resumed.run.usage.windows == 2
    assert resumed.run.usage.calls == 16
    assert [segment.revision for segment in track(resumed, reading_id).story] == [1]
    assert evaluator.calls[("propose", "reader")] == 2
    assert evaluator.calls[("propose", "writer")] == 2
    assert evaluator.calls[("propose", "keeper")] == 3
    assert verify_replay(resumed, store.read_events(run_id))["ok"]


async def test_new_reading_can_present_current_world_once_without_actor_calls(snapshot, settings, tmp_path):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    advanced = await runner.step(run_id)
    before_main = advanced.story[0].model_dump()
    added = runner.add_reading(run_id, "从读者开始", "reader", enabled=False)
    reading_id = new_track_id(advanced, added)
    assert added.run.usage.calls == advanced.run.usage.calls
    presented = await runner.present_reading(run_id, reading_id)
    assert presented.run.revision == advanced.run.revision == 1
    assert presented.run.usage.windows == advanced.run.usage.windows == 1
    assert presented.run.usage.calls == advanced.run.usage.calls + 1
    assert presented.story[0].model_dump() == before_main
    assert presented.positions == advanced.positions and presented.uses == advanced.uses
    assert presented.continuations == advanced.continuations
    assert track(presented, reading_id).story[0].focus_position_id == "reader"
    assert len([event for event in presented.events if event.type == "batch_installed"]) == 1
    repeated = await runner.present_reading(run_id, reading_id)
    assert repeated.model_dump() == presented.model_dump()
    assert store.load(run_id).model_dump() == presented.model_dump()
    assert verify_replay(presented, store.read_events(run_id))["ok"]


async def test_disabled_reading_keeps_history_and_resumes_on_a_later_world(snapshot, settings, tmp_path):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    added = runner.add_reading(run_id, "可暂停的阅读", "keeper")
    reading_id = new_track_id(snapshot, added)
    first = await runner.step(run_id)
    history = [segment.model_dump() for segment in track(first, reading_id).story]
    disabled = runner.set_reading_enabled(run_id, reading_id, False)
    assert disabled.run.usage == first.run.usage
    second = await runner.step(run_id)
    assert second.run.usage.calls == 15
    assert [segment.model_dump() for segment in track(second, reading_id).story] == history
    runner.set_reading_enabled(run_id, reading_id, True)
    third = await runner.step(run_id)
    assert [segment.revision for segment in track(third, reading_id).story] == [1, 3]
    assert third.run.usage.calls == 23
    assert verify_replay(third, store.read_events(run_id))["ok"]


async def test_cross_reading_history_reference_is_rejected_without_mutation(snapshot, settings, tmp_path):
    _, runner, run_id = runtime(snapshot, settings, tmp_path)
    advanced = await runner.step(run_id)
    state = runner.add_reading(run_id, "另一种承接", "keeper")
    reading_id = new_track_id(advanced, state)
    before = state.model_dump()
    invalid = Presentation(
        text="另一入口的段落仍然具有自己的来处。", focus_position_id="keeper",
        expression_ids=state.story[0].expression_ids,
        reinterprets=[state.story[0].id],
    )
    with pytest.raises(KernelError) as error:
        install_presentation(state, invalid, reading_id)
    assert error.value.code == "PRESENTATION_REFERENCE_INVALID"
    assert state.model_dump() == before


async def test_first_segment_must_honor_requested_start(snapshot, settings, tmp_path):
    _, runner, run_id = runtime(snapshot, settings, tmp_path)
    advanced = await runner.step(run_id)
    added = runner.add_reading(run_id, "固定首段入口", "keeper")
    reading_id = new_track_id(advanced, added)
    frame = runner.reading_frame(added, reading_id)
    assert frame.start_position_id == frame.required_focus_position_id == "keeper"
    before = added.model_dump()
    wrong_start = Presentation(
        text="错误入口不能悄悄成为首段。", focus_position_id="reader",
        expression_ids=advanced.story[0].expression_ids,
    )
    with pytest.raises(KernelError):
        install_presentation(added, wrong_start, reading_id)
    assert added.model_dump() == before


async def test_v1_snapshot_migrates_all_story_reading_and_pending_history(snapshot, settings, tmp_path):
    store, runner, run_id = runtime(snapshot, settings, tmp_path)
    await runner.step(run_id)
    current = await runner.step(run_id)
    legacy = current.model_dump()
    legacy.pop("reading_tracks")
    legacy["schema_version"] = 1
    legacy_proposal = (await FixtureProvider().propose(frame_for(current, "keeper"))).value
    legacy["pending"] = {
        "base_revision": 2, "proposals": {"keeper": legacy_proposal.model_dump()},
        "realizations": {}, "committed": False,
    }
    initial = next(event["data"]["initial"] for event in legacy["events"] if event["type"] == "created")
    initial.pop("reading_tracks", None)
    initial["schema_version"] = 1
    for event in legacy["events"]:
        if event["type"] == "presented":
            event["data"].pop("reading_id", None)
    migrated = Snapshot.model_validate(legacy)
    assert migrated.schema_version == 2 and len(migrated.reading_tracks) == 1
    assert migrated.story == current.story and migrated.reading == current.reading
    assert migrated.run.usage == current.run.usage
    assert migrated.pending.proposals["keeper"] == legacy_proposal and not migrated.pending.presentations
    assert migrated.story[1].reinterprets == [migrated.story[0].id]
    with store.connect() as database:
        database.execute("UPDATE runs SET snapshot=? WHERE id=?", (json.dumps(legacy), run_id))
        database.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        database.executemany("INSERT INTO events VALUES (?,?,?)", [
            (run_id, event["id"], json.dumps(event)) for event in legacy["events"]
        ])
    loaded_v1 = store.load(run_id)
    assert loaded_v1.story == current.story and loaded_v1.pending == migrated.pending
    assert verify_replay(loaded_v1, store.read_events(run_id))["ok"]
    store.save(migrated)
    restored = store.load(run_id)
    assert restored.story == current.story and restored.reading == current.reading
    assert verify_replay(restored, store.read_events(run_id))["ok"]


def test_http_reading_creation_toggle_and_same_world_idempotence(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        created = client.post("/api/runs", json={
            "preset_id": "letters", "provider": "fixture", "limits": {"max_tokens": 5120000},
        })
        assert created.status_code == 200
        original = created.json()
        path = f"/api/runs/{original['run']['id']}"
        added = client.post(path + "/readings", json={
            "title": "另一个入口", "start_position_id": "keeper", "enabled": True,
        })
        assert added.status_code == 200 and added.json()["run"]["usage"]["calls"] == 0
        other = next(item for item in added.json()["reading_tracks"] if item["id"] != "main")
        reading_path = path + f"/readings/{other['id']}"
        advanced = client.post(path + "/step")
        assert advanced.status_code == 200 and advanced.json()["run"]["usage"]["calls"] == 8
        repeated = client.post(reading_path + "/present")
        assert repeated.status_code == 200 and repeated.json() == advanced.json()
        disabled = client.patch(reading_path, json={"enabled": False})
        assert disabled.status_code == 200
        assert next(item for item in disabled.json()["reading_tracks"] if item["id"] == other["id"])["enabled"] is False
        next_window = client.post(path + "/step")
        assert next_window.status_code == 200 and next_window.json()["run"]["usage"]["calls"] == 15
        assert client.get(path + "/replay").json()["ok"]
        invalid = client.post(path + "/readings", json={
            "title": "无效入口", "start_position_id": "not_a_position",
        })
        assert invalid.status_code >= 400
        assert client.get(path).json() == next_window.json()
