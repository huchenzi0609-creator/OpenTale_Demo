"""Reference selection errors regenerate under one bounded, billed checkpoint.

Local evaluators exercise the scheduler without credentials or model services.
Reference repair is not a general exemption for invalid narrative operations.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import opentale.runner as runner_module
import pytest
from opentale.domain import Limits
from opentale.kernel import KernelError, frame_for
from opentale.providers import FixtureProvider, ModelResult, OutputFeedback, ProviderError
from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store

REFERENCE_FEEDBACK = OutputFeedback("PROPOSAL_REFERENCE", ("TARGET_UNKNOWN",))


class ReferenceFlakeProvider:
    """Return a fresh fixture after nominated, known-cost local rejections."""

    def __init__(
        self, failures=1, *, feedbacks=(REFERENCE_FEEDBACK,), phase="propose",
        parallel=3, usage=(2, 1), retryable=True, code="MODEL_OUTPUT_INVALID",
    ):
        self.fixture = FixtureProvider()
        self.failures = failures
        self.feedbacks = feedbacks
        self.phase = phase
        self.parallel = parallel
        self.usage = usage
        self.retryable = retryable
        self.code = code
        self.calls = Counter()
        self.inputs = {}
        self.entered = asyncio.Event()
        self.failure_pending = asyncio.Event()
        self.release_failure = asyncio.Event()
        self.repair_pending = asyncio.Event()
        self.release_repair = asyncio.Event()
        self.block_failure = False
        self.block_repair = False

    @property
    def failing_key(self):
        return self.phase, "main" if self.phase == "present" else "keeper"

    async def evaluate(self, phase, frame, repair):
        identifier = "main" if phase == "present" else frame.position_id
        key = phase, identifier
        self.calls[key] += 1
        self.inputs.setdefault(key, []).append((frame.model_dump(), repair))
        if phase == "propose" and self.parallel == 3:
            if sum(count for (stage, _), count in self.calls.items() if stage == "propose") >= 3:
                self.entered.set()
            await self.entered.wait()
        if key == self.failing_key and self.calls[key] == 1 and self.block_failure:
            self.failure_pending.set()
            await self.release_failure.wait()
        if key == self.failing_key and repair is not None and self.block_repair:
            self.repair_pending.set()
            await self.release_repair.wait()
        if key == self.failing_key and self.calls[key] <= self.failures:
            feedback = self.feedbacks[min(self.calls[key] - 1, len(self.feedbacks) - 1)]
            raise ProviderError(
                self.code, "离线测试中的承接选择错误。", self.retryable,
                usage=self.usage, feedback=feedback,
            )
        return ModelResult((await getattr(self.fixture, phase)(frame)).value, 2, 1)

    async def propose(self, frame, *, repair=None):
        return await self.evaluate("propose", frame, repair)

    async def realize(self, frame, *, repair=None):
        return await self.evaluate("realize", frame, repair)

    async def present(self, frame, *, repair=None):
        return await self.evaluate("present", frame, repair)


def runtime(snapshot, settings, tmp_path, monkeypatch, provider, *, calls=32):
    snapshot.run.provider = "deepseek"
    snapshot.run.limits = Limits(
        max_calls=calls, max_tokens=5120000, max_windows=1, max_parallel=provider.parallel,
    )
    store = Store(tmp_path / "runtime")
    store.save(snapshot)
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: provider)
    return store, Runner(store, settings), snapshot.run.id


def assert_completed(state, store, *, calls, failed):
    assert state.pending is None and state.run.revision == state.run.usage.windows == 1
    assert len(state.story) == 1
    assert state.run.usage.calls == calls and state.run.usage.failed_calls == failed
    assert state.run.usage.input_tokens == calls * 2 and state.run.usage.output_tokens == calls
    assert state.run.usage.reserved_tokens == state.run.usage.unknown_tokens == 0
    assert sum(event.type == "proposal_ready" for event in state.events) == 3
    assert sum(event.type == "batch_installed" for event in state.events) == 1
    assert all(continuation.resume_count == 1 for continuation in state.continuations)
    assert verify_replay(state, store.read_events(state.run.id))["ok"]


@pytest.mark.parametrize("reason", ["TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE", "INTERFACE_UNKNOWN"])
@pytest.mark.parametrize("parallel", [1, 3])
async def test_reference_selection_regenerates_same_frame_without_repeating_peers(
    snapshot, settings, tmp_path, monkeypatch, reason, parallel,
):
    feedback = OutputFeedback("PROPOSAL_REFERENCE", (reason,))
    provider = ReferenceFlakeProvider(feedbacks=(feedback,), parallel=parallel)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=8)
    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_completed(completed, store, calls=8, failed=1)
    initial, retry = provider.inputs[provider.failing_key]
    assert initial[0] == retry[0]
    assert initial[1] is None and retry[1] == feedback
    assert provider.calls[provider.failing_key] == 2
    assert all(count == 1 for key, count in provider.calls.items() if key != provider.failing_key)
    retries = [event for event in completed.events if event.type == "format_retry"]
    assert len(retries) == 1
    assert retries[0].data["category"] == "PROPOSAL_REFERENCE"
    assert retries[0].data["issues"] == [reason]
    assert "feedback" not in completed.run.model_dump()


async def test_reference_failure_stops_after_three_attempts_and_manual_resume_preserves_peers(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = ReferenceFlakeProvider(failures=3)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    interrupted = store.load(run_id)
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    assert not interrupted.pending.committed and interrupted.run.revision == 0
    assert interrupted.run.usage.calls == 5 and interrupted.run.usage.failed_calls == 3
    assert interrupted.run.usage.input_tokens == 10 and interrupted.run.usage.output_tokens == 5
    assert interrupted.run.usage.reserved_tokens == interrupted.run.usage.unknown_tokens == 0
    assert provider.calls[provider.failing_key] == 3
    assert [repair for _, repair in provider.inputs[provider.failing_key]] == [
        None, REFERENCE_FEEDBACK, REFERENCE_FEEDBACK,
    ]
    assert "feedback" not in interrupted.run.last_error
    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_completed(completed, store, calls=10, failed=3)
    assert provider.calls[("propose", "writer")] == provider.calls[("propose", "reader")] == 1


async def test_format_and_reference_errors_share_one_two_retry_ceiling(
    snapshot, settings, tmp_path, monkeypatch,
):
    feedbacks = (
        OutputFeedback("JSON_SYNTAX", ("INVALID_JSON",)),
        REFERENCE_FEEDBACK,
        OutputFeedback("SCHEMA_VALIDATION", ("fragments:missing",)),
    )
    provider = ReferenceFlakeProvider(failures=3, feedbacks=feedbacks)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    state = store.load(run_id)
    assert provider.calls[provider.failing_key] == 3
    assert [repair for _, repair in provider.inputs[provider.failing_key]] == [None, *feedbacks[:2]]
    assert len([event for event in state.events if event.type == "format_retry"]) == 2
    assert state.run.usage.calls == 5 and state.run.usage.failed_calls == 3


@pytest.mark.parametrize("parallel", [1, 3])
async def test_reference_retry_does_not_spend_calls_needed_to_finish_existing_window(
    snapshot, settings, tmp_path, monkeypatch, parallel,
):
    provider = ReferenceFlakeProvider(parallel=parallel)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider, calls=7)
    with pytest.raises((ProviderError, KernelError)):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    runner.update_limits(run_id, Limits(max_calls=8, max_tokens=5120000, max_windows=1, max_parallel=parallel))
    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert_completed(completed, store, calls=8, failed=1)


async def test_reference_retry_obeys_remaining_token_admission_and_settles_exact_rejection_usage(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = ReferenceFlakeProvider()
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    state = store.load(run_id)
    estimates = [runner._token_estimate(state, frame_for(state, position.id)) for position in state.positions]
    # Admit the three overlapping base requests. The rejected response's exact bill
    # leaves insufficient capacity for a new request, even once peers settle.
    state.run.limits.max_tokens = sum(estimates) + 100
    provider.usage = (state.run.limits.max_tokens - min(estimates) // 2, 0)
    store.save(state)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 1
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert interrupted.run.usage.input_tokens == provider.usage[0] + 4
    assert interrupted.run.usage.output_tokens == 2
    assert interrupted.run.usage.reserved_tokens == interrupted.run.usage.unknown_tokens == 0


async def test_pause_prevents_reference_regeneration_without_discarding_inflight_peers(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = ReferenceFlakeProvider()
    provider.block_failure = True
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.failure_pending.wait(), timeout=2)
    runner.pause(run_id)
    provider.release_failure.set()
    with pytest.raises(ProviderError):
        await asyncio.wait_for(task, timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 1
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert interrupted.run.usage.reserved_tokens == 0
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    assert not any(event.type == "format_retry" for event in interrupted.events)


async def test_cancelled_reference_request_is_billed_conservatively_and_not_retried_in_background(
    snapshot, settings, tmp_path, monkeypatch,
):
    provider = ReferenceFlakeProvider()
    provider.block_repair = True
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    task = asyncio.create_task(runner.step(run_id))
    await asyncio.wait_for(provider.repair_pending.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 2
    assert interrupted.run.usage.calls == 4 and interrupted.run.usage.failed_calls == 2
    assert interrupted.run.usage.input_tokens == 6 and interrupted.run.usage.output_tokens == 3
    assert interrupted.run.usage.reserved_tokens == 0 and interrupted.run.usage.unknown_tokens > 0
    assert set(interrupted.pending.proposals) == {"writer", "reader"}
    unknown = interrupted.run.usage.unknown_tokens
    provider.block_repair = False
    provider.release_repair.set()
    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert completed.run.usage.calls == 9 and completed.run.usage.failed_calls == 2
    assert completed.run.usage.input_tokens == 16 and completed.run.usage.output_tokens == 8
    assert completed.run.usage.unknown_tokens == unknown and completed.run.usage.reserved_tokens == 0
    assert provider.calls[provider.failing_key] == 3
    assert provider.calls[("propose", "writer")] == provider.calls[("propose", "reader")] == 1
    assert verify_replay(completed, store.read_events(run_id))["ok"]


@pytest.mark.parametrize("phase", ["realize", "present"])
async def test_proposal_reference_feedback_cannot_authorize_other_stage_retry(
    snapshot, settings, tmp_path, monkeypatch, phase,
):
    provider = ReferenceFlakeProvider(phase=phase)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 1
    assert not any(event.type == "format_retry" for event in interrupted.events)


@pytest.mark.parametrize("feedback,phase,retryable,code", [
    (OutputFeedback("PROPOSAL_INVALID", ("TARGET_UNKNOWN",)), "propose", True, "MODEL_OUTPUT_INVALID"),
    (OutputFeedback("READING_INVALID", ("READING_SCOPE_INVALID",)), "propose", True, "MODEL_OUTPUT_INVALID"),
    (OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)), "propose", True, "MODEL_OUTPUT_INVALID"),
    (REFERENCE_FEEDBACK, "propose", False, "MODEL_OUTPUT_INVALID"),
    (REFERENCE_FEEDBACK, "propose", True, "PROVIDER_TIMEOUT"),
    (None, "propose", True, "MODEL_OUTPUT_INVALID"),
])
async def test_semantic_errors_or_untrusted_feedback_do_not_authorize_reference_retry(
    snapshot, settings, tmp_path, monkeypatch, feedback, phase, retryable, code,
):
    provider = ReferenceFlakeProvider(phase=phase, feedbacks=(feedback,), retryable=retryable, code=code)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert caught.value.code == code
    interrupted = store.load(run_id)
    assert provider.calls[provider.failing_key] == 1
    assert not any(event.type == "format_retry" for event in interrupted.events)
    assert interrupted.run.usage.reserved_tokens == 0


@pytest.mark.parametrize("issues", [
    (), ("ANCHOR_NOT_IN_TEXT",), ("text:missing",),
    ("TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE"),
])
def test_reference_feedback_cannot_carry_unrelated_or_ambiguous_issue(issues):
    with pytest.raises(ValueError, match="Invalid safe output feedback"):
        OutputFeedback("PROPOSAL_REFERENCE", issues)
