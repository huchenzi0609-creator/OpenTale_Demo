"""Bounded format repairs are explicit billed calls against one unchanged frame.

Every evaluator here is local. Coroutine barriers exercise concurrent failures;
no test loads secret-bearing configuration or reaches a model service.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import opentale.runner as runner_module
import pytest
from opentale.domain import Limits
from opentale.kernel import KernelError
from opentale.providers import FixtureProvider, ModelResult, OutputFeedback, ProviderError
from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store

FORMAT_FEEDBACK = OutputFeedback("SCHEMA_VALIDATION", ("text:missing",))


class FormatFlakeProvider:
    """Fail nominated stages a known number of times, then return valid fixtures."""

    def __init__(
        self, failures, *, feedback=FORMAT_FEEDBACK, code="MODEL_OUTPUT_INVALID", barrier=False,
        failure_usage=(2, 1), retryable=True,
    ):
        self.fixture = FixtureProvider()
        self.failures = failures
        self.feedback = feedback
        self.code = code
        self.barrier = barrier
        self.failure_usage = failure_usage
        self.retryable = retryable
        self.entered = asyncio.Event()
        self.calls = Counter()
        self.inputs = {}

    async def evaluate(self, phase, frame, repair):
        identifier = (frame.start_position_id or "main") if phase == "present" else frame.position_id
        key = (phase, identifier)
        self.calls[key] += 1
        self.inputs.setdefault(key, []).append((frame.model_dump(), repair))
        if phase == "propose" and self.barrier:
            # The first three actors overlap. Later attempts find the barrier open.
            if sum(n for (stage, _), n in self.calls.items() if stage == "propose") >= 3:
                self.entered.set()
            await self.entered.wait()
        if self.calls[key] <= self.failures.get(key, 0):
            raise ProviderError(
                self.code, "离线测试中的格式错误。", self.retryable,
                usage=self.failure_usage, feedback=self.feedback,
            )
        value = (await getattr(self.fixture, phase)(frame)).value
        return ModelResult(value, 2, 1)

    async def propose(self, frame, *, repair=None):
        return await self.evaluate("propose", frame, repair)

    async def realize(self, frame, *, repair=None):
        return await self.evaluate("realize", frame, repair)

    async def present(self, frame, *, repair=None):
        return await self.evaluate("present", frame, repair)


def runtime(snapshot, settings, tmp_path, monkeypatch, provider, *, calls=32, parallel=3):
    snapshot.run.provider = "deepseek"
    snapshot.run.limits = Limits(max_calls=calls, max_tokens=5120000, max_windows=1, max_parallel=parallel)
    store = Store(tmp_path / "runtime")
    store.save(snapshot)
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: provider)
    return store, Runner(store, settings), snapshot.run.id


def assert_one_window(state, store, *, calls, failed):
    assert state.pending is None and state.run.revision == state.run.usage.windows == 1
    assert len(state.story) == 1
    assert state.run.usage.calls == calls and state.run.usage.failed_calls == failed
    assert state.run.usage.input_tokens == calls * 2 and state.run.usage.output_tokens == calls
    assert state.run.usage.unknown_tokens == state.run.usage.reserved_tokens == 0
    assert all(continuation.resume_count == 1 for continuation in state.continuations)
    assert sum(event.type == "proposal_ready" for event in state.events) == 3
    assert sum(event.type == "batch_installed" for event in state.events) == 1
    assert sum(event.type == "continuation_resumed" for event in state.events) == 3
    assert verify_replay(state, store.read_events(state.run.id))["ok"]


@pytest.mark.parametrize("phase,identifier", [("propose", "keeper"), ("realize", "keeper"), ("present", "main")])
@pytest.mark.parametrize("parallel", [1, 3])
async def test_single_format_failure_recovers_in_same_window_with_exact_accounting(
    snapshot, settings, tmp_path, monkeypatch, phase, identifier, parallel,
):
    key = (phase, identifier)
    provider = FormatFlakeProvider({key: 1}, barrier=parallel == 3)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=8, parallel=parallel)
    result = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_one_window(result, store, calls=8, failed=1)
    assert provider.calls[key] == 2
    first, second = provider.inputs[key]
    assert first[1] is None and second[1] == FORMAT_FEEDBACK
    assert first[0] == second[0]
    assert all(count == (2 if candidate == key else 1) for candidate, count in provider.calls.items())


async def test_concurrent_failures_repair_only_two_missing_actors(snapshot, settings, tmp_path, monkeypatch):
    failures = {("propose", "keeper"): 1, ("propose", "writer"): 1}
    provider = FormatFlakeProvider(failures, barrier=True)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=9)
    result = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_one_window(result, store, calls=9, failed=2)
    assert provider.calls[("propose", "reader")] == 1
    for key in failures:
        assert provider.calls[key] == 2 and provider.inputs[key][1][1] == FORMAT_FEEDBACK


async def test_three_bad_attempts_stop_and_manual_resume_does_not_repeat_successful_peers(
    snapshot, settings, tmp_path, monkeypatch,
):
    key = ("propose", "keeper")
    provider = FormatFlakeProvider({key: 3}, barrier=True)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    interrupted = store.load(run_id)
    assert interrupted.pending is not None and not interrupted.pending.committed
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    assert interrupted.run.usage.calls == 5 and interrupted.run.usage.failed_calls == 3
    assert interrupted.run.usage.input_tokens == 10 and interrupted.run.usage.output_tokens == 5
    assert interrupted.run.usage.unknown_tokens == interrupted.run.usage.reserved_tokens == 0
    assert provider.calls[key] == 3
    assert [repair for _, repair in provider.inputs[key]] == [None, FORMAT_FEEDBACK, FORMAT_FEEDBACK]
    assert "feedback" not in interrupted.run.last_error
    resumed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_one_window(resumed, store, calls=10, failed=3)
    assert provider.calls[("propose", "writer")] == provider.calls[("propose", "reader")] == 1


@pytest.mark.parametrize("parallel", [1, 3])
async def test_repair_does_not_consume_calls_required_for_unfinished_base_work(
    snapshot, settings, tmp_path, monkeypatch, parallel,
):
    key = ("propose", "keeper")
    provider = FormatFlakeProvider({key: 1}, barrier=parallel == 3)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=7, parallel=parallel)
    with pytest.raises((ProviderError, KernelError)):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[key] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    runner.update_limits(run_id, Limits(max_calls=8, max_tokens=5120000, max_windows=1, max_parallel=parallel))
    resumed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_one_window(resumed, store, calls=8, failed=1)


@pytest.mark.parametrize(
    "feedback,code",
    [
        (None, "MODEL_OUTPUT_INVALID"),
        (OutputFeedback("PROPOSAL_INVALID"), "MODEL_OUTPUT_INVALID"),
        (OutputFeedback("READING_INVALID"), "MODEL_OUTPUT_INVALID"),
        (OutputFeedback("PROSE_INVALID"), "MODEL_OUTPUT_INVALID"),
        (FORMAT_FEEDBACK, "PROVIDER_TIMEOUT"),
    ],
)
async def test_only_explicit_format_errors_are_repaired_automatically(
    snapshot, settings, tmp_path, monkeypatch, feedback, code,
):
    key = ("propose", "keeper")
    provider = FormatFlakeProvider({key: 1}, feedback=feedback, code=code)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError) as caught:
        await runner.step(run_id)
    assert caught.value.code == code
    interrupted = store.load(run_id)
    assert provider.calls[key] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert set(interrupted.pending.proposals) == {"writer", "reader"}


async def test_additional_reader_repairs_without_repeating_main_presentation_or_actors(
    snapshot, settings, tmp_path, monkeypatch,
):
    key = ("present", "keeper")
    provider = FormatFlakeProvider({key: 1}, barrier=True)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=9)
    added = runner.add_reading(run_id, "离线补救的读法", "keeper")
    reading_id = added.reading_tracks[-1].id
    result = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_one_window(result, store, calls=9, failed=1)
    assert len(next(track for track in result.reading_tracks if track.id == reading_id).story) == 1
    assert provider.calls[("present", "main")] == 1 and provider.calls[key] == 2


async def test_manual_reading_repair_only_spends_current_reading_calls(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = FormatFlakeProvider({})
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=9)
    completed = await runner.step(run_id)
    added = runner.add_reading(run_id, "不重跑人物的读法", "keeper", enabled=False)
    reading_id = added.reading_tracks[-1].id
    provider.failures[("present", "keeper")] = 1
    before_calls = provider.calls.copy()
    result = await runner.present_reading(run_id, reading_id)
    assert result.run.usage.calls == 9 and result.run.usage.failed_calls == 1
    assert result.run.usage.windows == 1 and result.run.revision == 1
    assert result.run.status == completed.run.status and result.run.phase == completed.run.phase
    assert result.continuations == completed.continuations
    assert provider.calls[("present", "keeper")] == 2
    for key, count in before_calls.items():
        assert provider.calls[key] == count
    assert verify_replay(result, store.read_events(run_id))["ok"]


@pytest.mark.parametrize("category", ["JSON_SYNTAX", "JSON_TYPE", "SCHEMA_VALIDATION"])
async def test_all_format_categories_support_safe_bounded_repair(
    snapshot, settings, tmp_path, monkeypatch, category,
):
    provider = FormatFlakeProvider({("propose", "keeper"): 1}, feedback=OutputFeedback(category))
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=8)
    result = await runner.step(run_id)
    assert_one_window(result, store, calls=8, failed=1)
    assert provider.inputs[("propose", "keeper")][1][1].category == category


async def test_nonretryable_error_is_not_repaired_even_with_format_feedback(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = FormatFlakeProvider({("propose", "keeper"): 1}, retryable=False)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError):
        await runner.step(run_id)
    assert provider.calls[("propose", "keeper")] == 1
    assert store.load(run_id).run.usage.calls == 3


async def test_token_budget_blocks_extra_repair_without_discarding_successful_peers(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = FormatFlakeProvider({("propose", "keeper"): 1}, barrier=True, failure_usage=(20000, 0))
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    state = store.load(run_id)
    # Enough for the three overlapping first admissions (about 25K reserved),
    # but not another generation after the rejected response's exact usage arrives.
    state.run.limits.max_tokens = 28000
    store.save(state)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[("propose", "keeper")] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert interrupted.run.usage.input_tokens == 20004 and interrupted.run.usage.output_tokens == 2
    assert interrupted.run.usage.unknown_tokens == interrupted.run.usage.reserved_tokens == 0
    assert set(interrupted.pending.proposals) == {"writer", "reader"}


class PausedFailureProvider(FormatFlakeProvider):
    def __init__(self):
        super().__init__({("propose", "keeper"): 1}, barrier=True)
        self.failure_pending = asyncio.Event()
        self.release_failure = asyncio.Event()

    async def propose(self, frame, *, repair=None):
        if frame.position_id == "keeper" and self.calls[("propose", "keeper")] == 0:
            self.failure_pending.set()
            await self.release_failure.wait()
        return await super().propose(frame, repair=repair)


async def test_user_pause_prevents_dispatching_format_repair(snapshot, settings, tmp_path, monkeypatch):
    provider = PausedFailureProvider()
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.failure_pending.wait(), timeout=2)
    runner.pause(run_id)
    provider.release_failure.set()
    with pytest.raises(ProviderError):
        await asyncio.wait_for(task, timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[("propose", "keeper")] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    assert interrupted.run.usage.reserved_tokens == 0


class CancelledRepairProvider(FormatFlakeProvider):
    def __init__(self):
        super().__init__({("propose", "keeper"): 1}, barrier=True)
        self.repair_pending = asyncio.Event()
        self.release_repair = asyncio.Event()

    async def propose(self, frame, *, repair=None):
        if frame.position_id == "keeper" and repair is not None:
            # Count a dispatched request even when cancelled before a response.
            self.calls[("propose", "keeper")] += 1
            self.inputs[("propose", "keeper")].append((frame.model_dump(), repair))
            self.repair_pending.set()
            await self.release_repair.wait()
            return ModelResult((await self.fixture.propose(frame)).value, 2, 1)
        return await super().propose(frame, repair=repair)


async def test_cancelled_repair_settles_unknown_usage_once_and_does_not_dispatch_third_attempt(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = CancelledRepairProvider()
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.repair_pending.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupted = store.load(run_id)
    assert provider.calls[("propose", "keeper")] == 2
    assert interrupted.run.usage.calls == 4 and interrupted.run.usage.failed_calls == 2
    assert interrupted.run.usage.input_tokens == 6 and interrupted.run.usage.output_tokens == 3
    assert interrupted.run.usage.reserved_tokens == 0 and interrupted.run.usage.unknown_tokens > 0
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    unknown = interrupted.run.usage.unknown_tokens
    provider.release_repair.set()
    resumed = await runner.step(run_id)
    assert resumed.run.usage.calls == 9 and resumed.run.usage.failed_calls == 2
    assert resumed.run.usage.input_tokens == 16 and resumed.run.usage.output_tokens == 8
    assert resumed.run.usage.unknown_tokens == unknown and resumed.run.usage.reserved_tokens == 0
    assert provider.calls[("propose", "keeper")] == 3
    assert provider.calls[("propose", "writer")] == provider.calls[("propose", "reader")] == 1
    assert verify_replay(resumed, store.read_events(run_id))["ok"]
