"""Reading-reference recovery preserves accepted work; unlimited is explicit and accounted."""

from collections import Counter

import pytest
from opentale.capacity import budget_bounds
from opentale.domain import Limits
from opentale.kernel import KernelError, fingerprint
from opentale.providers import FixtureProvider, ProviderError
from opentale.providers.feedback import OutputFeedback
from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store


class ReadingRepairs(FixtureProvider):
    def __init__(self, failures, reason):
        self.failures = failures
        self.reason = reason
        self.jobs = Counter()
        self.frames = []

    async def propose(self, frame, **kwargs):
        self.jobs[("propose", frame.position_id)] += 1
        return await super().propose(frame, **kwargs)

    async def realize(self, frame, **kwargs):
        self.jobs[("realize", frame.position_id)] += 1
        return await super().realize(frame, **kwargs)

    async def present(self, frame, *, repair=None):
        self.jobs[("present", "reading")] += 1
        self.frames.append((fingerprint(frame.model_dump()), repair))
        if self.failures:
            self.failures -= 1
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", "阅读引用无效。", True, usage=(12, 8),
                feedback=OutputFeedback("READING_INVALID", (self.reason,)),
            )
        return await super().present(frame, repair=repair)


@pytest.mark.parametrize("reason", [
    "READING_SCOPE_INVALID", "READING_FOCUS_INVALID", "READING_EXPRESSION_INVALID",
    "READING_HISTORY_INVALID", "READING_SHARED_REQUIRED",
])
async def test_reading_reference_repair_is_funded_and_uses_same_frame(snapshot, settings, tmp_path, monkeypatch, reason):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(**budget_bounds())
    store.save(snapshot)
    provider = ReadingRepairs(1, reason)
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: provider)
    state = await Runner(store, settings).step(snapshot.run.id)
    assert state.run.usage.calls == 8 and state.run.usage.failed_calls == 1
    assert len(state.story) == 1 and state.pending is None
    assert provider.frames[0][0] == provider.frames[1][0]
    assert provider.frames[1][1] == OutputFeedback("READING_INVALID", (reason,))
    assert all(count == 1 for job, count in provider.jobs.items() if job[0] != "present")
    assert verify_replay(state, store.read_events(state.run.id))["ok"]


async def test_bad_reading_stops_after_two_repairs_and_resumes_without_actor_replay(snapshot, settings, tmp_path, monkeypatch):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(**budget_bounds())
    store.save(snapshot)
    provider = ReadingRepairs(3, "READING_HISTORY_INVALID")
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: provider)
    runner = Runner(store, settings)
    with pytest.raises(ProviderError):
        await runner.step(snapshot.run.id)
    saved = store.load(snapshot.run.id)
    assert saved.pending.committed and len(saved.pending.realizations) == 3
    assert provider.jobs[("present", "reading")] == 3
    assert saved.run.usage.calls == 9 and saved.run.status == "error"
    state = await runner.step(snapshot.run.id)
    assert state.run.usage.calls == 10 and state.pending is None
    assert all(count == 1 for job, count in provider.jobs.items() if job[0] != "present")
    assert len({value for value, _ in provider.frames}) == 1


async def test_no_spare_repair_budget_then_unlimited_update_preserves_checkpoint(snapshot, settings, tmp_path, monkeypatch):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(max_calls=7)
    store.save(snapshot)
    provider = ReadingRepairs(1, "READING_EXPRESSION_INVALID")
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: provider)
    runner = Runner(store, settings)
    with pytest.raises(ProviderError):
        await runner.step(snapshot.run.id)
    failed = store.load(snapshot.run.id)
    assert failed.run.usage.calls == 7 and len(provider.frames) == 1
    updated = runner.update_limits(snapshot.run.id, Limits(**budget_bounds()))
    assert updated.pending == failed.pending and updated.run.usage == failed.run.usage
    assert updated.run.last_error == failed.run.last_error
    completed = await runner.step(snapshot.run.id)
    assert completed.run.usage.calls == 8 and completed.pending is None
    assert verify_replay(completed, store.read_events(completed.run.id))["ok"]


async def test_unlimited_accepts_usage_beyond_every_old_budget_without_hidden_caps(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(**budget_bounds())
    snapshot.run.usage.calls = 1001
    snapshot.run.usage.windows = 1001
    snapshot.run.usage.input_tokens = 512000001
    store.save(snapshot)
    runner = Runner(store, settings)
    state = await runner.step(snapshot.run.id)
    assert state.run.usage.calls == 1008 and state.run.usage.windows == 1002
    assert state.run.usage.input_tokens == 512000001
    with pytest.raises(KernelError, match="已用"):
        runner.update_limits(snapshot.run.id, Limits(max_tokens=512000000))


def test_large_verified_usage_is_not_rejected_as_a_budget_ceiling():
    error = ProviderError("MODEL_OUTPUT_INVALID", "离线测试", True, usage=(6000000, 9000000))
    assert error.usage == (6000000, 9000000)
