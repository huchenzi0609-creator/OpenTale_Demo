"""Rejected outputs retain exact billing and recover without repeating accepted drafts."""

from __future__ import annotations

import asyncio
from collections import Counter

import opentale.runner as runner_module
import pytest
from opentale.domain import Limits
from opentale.providers import FixtureProvider, ModelResult, ProviderError
from opentale.runner import Runner
from opentale.storage import Store


class RejectOnceProvider:
    """A concurrent, entirely local evaluator with one rejected draft."""

    def __init__(self, *, failure_usage: tuple[int, int] | None):
        self.fixture = FixtureProvider()
        self.failure_usage = failure_usage
        self.calls = Counter()
        self.initial_drafts = asyncio.Event()
        self.failed_frame = None

    async def propose(self, frame):
        self.calls[("propose", frame.position_id)] += 1
        # All three initial drafts must enter before any of them can complete.
        if sum(count for (phase, _), count in self.calls.items() if phase == "propose") == 3:
            self.initial_drafts.set()
        await self.initial_drafts.wait()
        if frame.position_id == "keeper" and self.calls[("propose", "keeper")] == 1:
            self.failed_frame = frame.model_copy(deep=True)
            code = "MODEL_OUTPUT_INVALID" if self.failure_usage is not None else "PROVIDER_TIMEOUT"
            raise ProviderError(code, "离线测试中的一次生成失败。", True, usage=self.failure_usage)
        result = await self.fixture.propose(frame)
        return ModelResult(result.value, 2, 1)

    async def realize(self, frame):
        self.calls[("realize", frame.position_id)] += 1
        result = await self.fixture.realize(frame)
        return ModelResult(result.value, 2, 1)

    async def present(self, frame):
        self.calls[("present", "reading")] += 1
        result = await self.fixture.present(frame)
        return ModelResult(result.value, 2, 1)


def runtime(snapshot, settings, tmp_path, monkeypatch, provider):
    assert len(snapshot.positions) == 3
    snapshot.run.provider = "deepseek"
    snapshot.run.limits = Limits(max_calls=8, max_tokens=500000, max_parallel=3, max_windows=1)
    store = Store(tmp_path / "runtime")
    store.save(snapshot)
    monkeypatch.setattr(runner_module, "make_provider", lambda mode, config: provider)
    return store, Runner(store, settings), snapshot.run.id


@pytest.mark.parametrize("usage", [None, (0, 0), (23, 11)])
def test_provider_error_known_usage_is_internal_only(usage):
    error = ProviderError("MODEL_OUTPUT_INVALID", "生成格式不符合要求。", True, usage=usage)
    assert error.usage == usage
    assert error.public() == {
        "code": "MODEL_OUTPUT_INVALID",
        "message": "生成格式不符合要求。",
        "retryable": True,
    }


async def test_rejected_draft_with_known_usage_preserves_peers_and_fits_exact_call_budget(
    snapshot, settings, tmp_path, monkeypatch
):
    provider = RejectOnceProvider(failure_usage=(2, 1))
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)

    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    interrupted = store.load(run_id)
    assert interrupted.pending is not None and not interrupted.pending.committed
    assert set(interrupted.pending.proposals) == {p.id for p in snapshot.positions} - {"keeper"}
    assert interrupted.run.revision == 0 and not interrupted.story
    assert interrupted.run.usage.calls == 3 and interrupted.run.usage.failed_calls == 1
    assert interrupted.run.usage.input_tokens == 6 and interrupted.run.usage.output_tokens == 3
    assert interrupted.run.usage.reserved_tokens == 0 and interrupted.run.usage.unknown_tokens == 0
    assert interrupted.run.last_error == caught.value.public()

    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert completed.pending is None and completed.run.revision == 1
    assert completed.run.usage.windows == 1 and len(completed.story) == 1
    assert completed.run.usage.calls == completed.run.limits.max_calls == 8
    assert completed.run.usage.failed_calls == 1
    assert completed.run.usage.input_tokens == 16 and completed.run.usage.output_tokens == 8
    assert completed.run.usage.reserved_tokens == 0 and completed.run.usage.unknown_tokens == 0
    for position in completed.positions:
        assert provider.calls[("propose", position.id)] == (2 if position.id == "keeper" else 1)
        assert provider.calls[("realize", position.id)] == 1
    assert provider.calls[("present", "reading")] == 1
    assert sum(event.type == "proposal_ready" for event in completed.events) == 3
    assert sum(event.type == "batch_installed" for event in completed.events) == 1


async def test_unknown_network_usage_keeps_conservative_charge_during_recovery(
    snapshot, settings, tmp_path, monkeypatch
):
    provider = RejectOnceProvider(failure_usage=None)
    store, runner, run_id = runtime(snapshot, settings, tmp_path, monkeypatch, provider)

    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert caught.value.code == "PROVIDER_TIMEOUT"
    interrupted = store.load(run_id)
    assert provider.failed_frame is not None
    estimate = len(provider.failed_frame.model_dump_json().encode("utf-8")) + 4096 + settings.max_output_tokens
    assert interrupted.run.usage.unknown_tokens == estimate > 0
    assert interrupted.run.usage.input_tokens == 4 and interrupted.run.usage.output_tokens == 2
    assert interrupted.run.usage.reserved_tokens == 0 and interrupted.run.usage.failed_calls == 1

    completed = await asyncio.wait_for(runner.step(run_id), timeout=3)
    assert completed.run.usage.calls == 8 and completed.run.usage.failed_calls == 1
    assert completed.run.usage.input_tokens == 14 and completed.run.usage.output_tokens == 7
    assert completed.run.usage.unknown_tokens == estimate
    assert completed.run.usage.reserved_tokens == 0
    assert completed.run.usage.windows == 1 and len(completed.story) == 1
