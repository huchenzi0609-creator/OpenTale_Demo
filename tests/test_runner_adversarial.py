"""Interruptions and adversarial evaluators use dummy settings and zero network access."""

from __future__ import annotations

import asyncio
import json
import traceback
from collections import Counter
from pathlib import Path

import opentale.runner as runner_module
import pytest
from opentale.config import Settings
from opentale.domain import Limits
from opentale.presets import initialize, read_presets
from opentale.providers import FixtureProvider, ModelResult, ProviderError
from opentale.runner import Runner
from opentale.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class ControlledProvider:
    def __init__(self, fail_phase=None, fail_position="keeper", block_realize=False, block_present=False):
        self.fixture = FixtureProvider()
        self.fail_phase = fail_phase
        self.fail_position = fail_position
        self.failure_used = False
        self.block_realize = block_realize
        self.block_present = block_present
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = Counter()
        self.frames = []

    def safe_frame(self, frame):
        self.frames.append(frame.model_copy(deep=True))
        actor_content = json.dumps(frame.active_uses)
        assert "source_position_id" not in actor_content
        assert "source_expression_id" not in actor_content
        assert "unit-test-only-credential" not in actor_content

    async def propose(self, frame):
        self.calls[("propose", frame.position_id)] += 1
        self.safe_frame(frame)
        value = (await self.fixture.propose(frame)).value
        return ModelResult(value, 2, 1)

    async def realize(self, frame):
        self.calls[("realize", frame.position_id)] += 1
        self.safe_frame(frame)
        if self.block_realize:
            self.entered.set()
            await self.release.wait()
        if self.fail_phase == "realize" and frame.position_id == self.fail_position and not self.failure_used:
            self.failure_used = True
            raise ProviderError("MOCK_TEMPORARY_FAILURE", "测试中的临时失败。", True)
        value = (await self.fixture.realize(frame)).value
        return ModelResult(value, 2, 1)

    async def present(self, frame):
        self.calls[("present", "reading")] += 1
        if self.block_present:
            self.entered.set()
            await self.release.wait()
        if self.fail_phase == "present" and not self.failure_used:
            self.failure_used = True
            raise ProviderError("MOCK_TEMPORARY_FAILURE", "测试中的临时失败。", True)
        value = (await self.fixture.present(frame)).value
        return ModelResult(value, 2, 1)


def make_runtime(tmp_path, monkeypatch, provider, *, parallel=3):
    store = Store(tmp_path / "runtime")
    preset = next(item for item in read_presets(ROOT) if item["id"] == "letters")
    state = initialize(preset, "deepseek", Limits(max_calls=100, max_tokens=500000, max_parallel=parallel))
    store.save(state)
    monkeypatch.setattr(runner_module, "make_provider", lambda mode, settings: provider)
    runner = Runner(store, Settings(api_key="unit-test-only-credential"))
    return store, runner, state.run.id


@pytest.mark.parametrize("phase", ["realize", "present"])
async def test_failed_realization_or_presentation_resumes_without_repeating_committed_work(tmp_path, monkeypatch, phase):
    provider = ControlledProvider(fail_phase=phase)
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError, match="测试中的临时失败"):
        await runner.step(run_id)
    interrupted = store.load(run_id)
    assert interrupted.pending.committed
    assert interrupted.run.revision == 1
    assert len([event for event in interrupted.events if event.type == "batch_installed"]) == 1
    assert interrupted.run.usage.unknown_tokens > 0
    assert interrupted.run.usage.reserved_tokens == 0
    resumed = await runner.step(run_id)
    assert resumed.run.revision == 1 and resumed.pending is None
    assert resumed.run.usage.windows == 1 and len(resumed.story) == 1
    assert resumed.run.usage.calls == 8 and resumed.run.usage.failed_calls == 1
    assert resumed.run.usage.input_tokens == 14 and resumed.run.usage.output_tokens == 7
    assert all(provider.calls[("propose", position.id)] == 1 for position in resumed.positions)
    assert all(continuation.resume_count == 1 for continuation in resumed.continuations)
    assert len([event for event in resumed.events if event.type == "batch_installed"]) == 1
    if phase == "realize":
        assert provider.calls[("realize", "keeper")] == 2
        assert provider.calls[("present", "reading")] == 1
    else:
        assert all(provider.calls[("realize", position.id)] == 1 for position in resumed.positions)
        assert provider.calls[("present", "reading")] == 2


async def test_cancelled_realizers_release_reservations_and_resume_saved_batch(tmp_path, monkeypatch):
    provider = ControlledProvider(block_realize=True)
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    live = store.load(run_id)
    assert live.pending.committed and live.run.usage.reserved_tokens > 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupted = store.load(run_id)
    assert interrupted.run.status == "paused"
    assert interrupted.run.usage.reserved_tokens == 0
    assert interrupted.run.usage.unknown_tokens > 0
    provider.block_realize = False
    resumed = await runner.step(run_id)
    assert len(resumed.story) == 1 and resumed.run.revision == 1
    assert all(provider.calls[("propose", position.id)] == 1 for position in resumed.positions)
    assert all(continuation.resume_count == 1 for continuation in resumed.continuations)


async def test_hard_restart_conservatively_charges_persisted_inflight_reservations_once(tmp_path, monkeypatch):
    provider = ControlledProvider(block_realize=True)
    original, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    snapshot = original.load(run_id)
    assert snapshot.pending.committed and snapshot.run.usage.reserved_tokens > 0
    # A detached checkpoint models the last durable state of a killed process.
    recovered = Store(tmp_path / "restarted-runtime")
    recovered.save(snapshot)
    reservation = snapshot.run.usage.reserved_tokens
    recovered.recover()
    first = recovered.load(run_id)
    assert first.run.status == "paused" and first.run.usage.reserved_tokens == 0
    assert first.run.usage.unknown_tokens == reservation
    recovered.recover()
    assert recovered.load(run_id).run.usage.unknown_tokens == reservation
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider.block_realize = False
    restarted = Runner(recovered, Settings(api_key="unit-test-only-credential"))
    result = await restarted.step(run_id)
    assert result.run.revision == 1 and len(result.story) == 1
    assert result.run.usage.unknown_tokens == reservation


async def test_pause_during_realization_preserves_results_for_presentation_only_resume(tmp_path, monkeypatch):
    provider = ControlledProvider(block_realize=True)
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider, parallel=1)
    await runner.start(run_id)
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    runner.pause(run_id)
    provider.release.set()
    await asyncio.wait_for(runner.tasks[run_id], timeout=2)
    paused = store.load(run_id)
    assert paused.run.status == "paused" and paused.run.phase == "presenting"
    assert len(paused.pending.realizations) == len(paused.positions)
    assert provider.calls[("present", "reading")] == 0
    before = provider.calls.copy()
    result = await runner.step(run_id)
    assert len(result.story) == 1 and result.run.revision == 1
    for position in result.positions:
        assert provider.calls[("realize", position.id)] == before[("realize", position.id)]
        assert provider.calls[("propose", position.id)] == before[("propose", position.id)]


async def test_pause_during_presentation_finishes_one_segment_and_reports_paused(tmp_path, monkeypatch):
    provider = ControlledProvider(block_present=True)
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    await runner.start(run_id)
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    runner.pause(run_id)
    provider.release.set()
    await asyncio.wait_for(runner.tasks[run_id], timeout=2)
    paused = store.load(run_id)
    assert len(paused.story) == 1 and paused.pending is None
    assert paused.run.status == "paused" and paused.run.phase == "idle"
    assert paused.run.usage.windows == 1


async def test_running_step_rejects_start_and_second_step_without_extra_calls(tmp_path, monkeypatch):
    provider = ControlledProvider(block_realize=True)
    _, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    before = provider.calls.copy()
    with pytest.raises(runner_module.KernelError) as caught:
        await runner.start(run_id)
    assert caught.value.code == "RUN_BUSY"
    with pytest.raises(runner_module.KernelError) as caught:
        await runner.step(run_id)
    assert caught.value.code == "RUN_BUSY" and provider.calls == before
    provider.release.set()
    await task


async def test_known_usage_and_accepted_results_are_persisted_in_same_checkpoint(tmp_path, monkeypatch):
    provider = ControlledProvider()
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    checkpoints = []
    save = store.save

    def track_save(state):
        checkpoints.append(state.model_copy(deep=True))
        save(state)

    monkeypatch.setattr(store, "save", track_save)
    await runner.step(run_id)
    for checkpoint in checkpoints:
        if checkpoint.run.phase == "drafting" and checkpoint.pending is not None:
            assert checkpoint.run.usage.input_tokens == 2 * len(checkpoint.pending.proposals)
        elif checkpoint.run.phase == "realizing" and checkpoint.pending is not None:
            accepted = len(checkpoint.pending.proposals) + len(checkpoint.pending.realizations)
            assert checkpoint.run.usage.input_tokens == 2 * accepted


async def test_unexpected_evaluator_error_has_no_secret_bearing_public_exception_chain(tmp_path, monkeypatch):
    provider = ControlledProvider()
    store, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)

    async def crash(frame):
        raise ValueError("private-provider-body unit-test-only-credential")

    monkeypatch.setattr(provider, "realize", crash)
    with pytest.raises(ProviderError) as caught:
        await runner.step(run_id)
    visible_trace = "".join(traceback.format_exception(caught.value))
    assert "unit-test-only-credential" not in visible_trace
    assert "private-provider-body" not in visible_trace
    public = store.load(run_id).model_dump_json()
    assert "unit-test-only-credential" not in public and "private-provider-body" not in public


async def test_reading_frame_exposes_use_scopes_without_technical_creator_provenance(tmp_path, monkeypatch):
    provider = ControlledProvider()
    _, runner, run_id = make_runtime(tmp_path, monkeypatch, provider)
    state = await runner.step(run_id)
    frame = runner.reading_frame(state)
    serialized = frame.model_dump_json()
    assert '"sources"' not in serialized
    assert '"source_position_id"' not in serialized
    assert '"source_expression_id"' not in serialized
    assert frame.open_relations
