"""Context projection recovery preserves accepted work and immutable narrative inputs.

The providers below are deterministic fixtures. They deliberately model a local
projection refusal and an old in-flight request without loading configuration,
opening a network connection, or using any private experiment materials.
"""

from __future__ import annotations

from collections import Counter

import pytest
from opentale.domain import Limits
from opentale.kernel import fingerprint, semantic_projection
from opentale.presets import initialize
from opentale.providers import FixtureProvider, ProviderError
from opentale.reflection import position_scope
from opentale.replay import verify_replay
from opentale.request_evidence import verify_request_evidence
from opentale.runner import Runner
from opentale.storage import Store

OLD_PROJECTION = "provider-json-v1"
COMPACT_PROJECTION = "provider-json-v2-context"


def synthetic_state(count: int, mode: str = "reflexive_v2"):
    return initialize(
        {
            "id": "context-recovery",
            "name": "输入恢复测试",
            "positions": [
                {"id": f"person_{index}", "name": f"叙述位置{index}",
                 "seed": f"她记得第{index}次整理庭院，第二天还要去照料树苗。"}
                for index in range(count)
            ],
            "expressions": [{"id": "garden", "text": "庭院里的树苗已经需要移栽了。", "role": "共同生活"}],
        },
        "fixture", Limits(max_calls=None, max_tokens=None, max_windows=None, max_parallel=3),
        mode,
    )


class VersionedFixture(FixtureProvider):
    """Each projection version has a distinct, deterministic request body."""

    def __init__(self, version: str, *, reject_position: str | None = None,
                 fail_once: set[str] | None = None,
                 calls: list[tuple[str, str, str]] | None = None,
                 adapters: list[str] | None = None):
        self.projection_version = version
        self.reject_position = reject_position
        self.fail_once = fail_once if fail_once is not None else set()
        self.calls = calls if calls is not None else []
        self.adapters = adapters if adapters is not None else []

    def with_projection_version(self, version: str):
        assert version in {OLD_PROJECTION, COMPACT_PROJECTION}
        self.adapters.append(version)
        return VersionedFixture(version, fail_once=self.fail_once,
                                calls=self.calls, adapters=self.adapters)

    def request_projection(self, frame, method, *, repair=None):
        if method == "propose" and frame.position_id == self.reject_position:
            raise ProviderError("CONTEXT_TOO_LARGE", "合成输入在传输前被拒绝。")
        return {**super().request_projection(frame, method, repair=repair),
                "synthetic_projection_version": self.projection_version}

    async def propose(self, frame, **kwargs):
        self.calls.append(("propose", frame.position_id, self.projection_version))
        if frame.position_id in self.fail_once:
            self.fail_once.remove(frame.position_id)
            raise ProviderError("MODEL_TIMEOUT", "合成传输失败。", True, usage=(0, 0))
        return await super().propose(frame, **kwargs)

    async def realize(self, frame, **kwargs):
        self.calls.append(("realize", frame.position_id, self.projection_version))
        return await super().realize(frame, **kwargs)

    async def present(self, frame, **kwargs):
        self.calls.append(("present", "main", self.projection_version))
        return await super().present(frame, **kwargs)


@pytest.mark.parametrize("mode", ["classic", "reflexive", "reflexive_v2"])
async def test_preflight_refusal_has_no_call_or_unknown_usage_in_any_mode(mode, tmp_path, settings, monkeypatch):
    state = synthetic_state(3, mode)
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    provider = VersionedFixture(OLD_PROJECTION, reject_position="person_2")
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: provider)
    try:
        with pytest.raises(ProviderError) as caught:
            await runner.step(state.run.id)
        assert caught.value.code == "CONTEXT_TOO_LARGE"
        failed = store.load(state.run.id)
        assert len(failed.pending.proposals) == 2
        assert failed.run.usage.calls == len(provider.calls) == 2
        assert failed.run.usage.failed_calls == 0
        assert failed.run.usage.input_tokens == failed.run.usage.output_tokens == 0
        assert failed.run.usage.unknown_tokens == failed.run.usage.reserved_tokens == 0
    finally:
        await runner.shutdown()


@pytest.mark.parametrize("count", [3, 8])
async def test_local_context_refusal_resumes_only_missing_proposal(
    count, tmp_path, settings, monkeypatch,
):
    state = synthetic_state(count)
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    victim = f"person_{count - 1}"
    old = VersionedFixture(OLD_PROJECTION, reject_position=victim)
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: old)
    try:
        with pytest.raises(ProviderError) as caught:
            await runner.step(state.run.id)
        assert caught.value.code == "CONTEXT_TOO_LARGE"
        failed = store.load(state.run.id)
        assert failed.pending is not None and not failed.pending.committed
        assert len(failed.pending.proposals) == count - 1
        assert failed.run.usage.calls == len(old.calls) == count - 1
        assert failed.run.usage.failed_calls == 0
        assert failed.run.usage.unknown_tokens == failed.run.usage.reserved_tokens == 0
        assert all(request.scope_id != position_scope(victim) for request in failed.requests)
        assert verify_request_evidence(failed)
        original_frames = {
            key: fingerprint(frame.model_dump()) for key, frame in failed.pending.proposal_frames.items()
        }
        accepted_before = {request.id: request.model_dump() for request in failed.requests}
        accepted_events = [event.model_dump() for event in failed.events if event.type == "proposal_ready"]

        # Repeated local refusals must not consume a call or repeat a sibling's accepted proposal.
        with pytest.raises(ProviderError):
            await runner.step(state.run.id)
        refused_again = store.load(state.run.id)
        assert refused_again.run.usage == failed.run.usage
        assert len(old.calls) == count - 1
        assert {request.id: request.model_dump() for request in refused_again.requests} == accepted_before

        compact = VersionedFixture(COMPACT_PROJECTION)
        monkeypatch.setattr("opentale.runner.make_provider", lambda *_: compact)
        completed = await runner.step(state.run.id)

        assert completed.pending is None and completed.run.revision == 1
        assert [(stage, position) for stage, position, _ in compact.calls if stage == "propose"] == [
            ("propose", victim),
        ]
        assert Counter(stage for stage, _, _ in compact.calls) == {
            "propose": 1, "realize": count, "present": 1,
        }
        assert completed.run.usage.calls == count * 2 + 1
        assert completed.run.usage.unknown_tokens == completed.run.usage.reserved_tokens == 0
        assert all(request.model_dump() == accepted_before[request.id]
                   for request in completed.requests if request.id in accepted_before)
        assert [event.model_dump() for event in completed.events if event.type == "proposal_ready"][:count - 1] \
            == accepted_events
        proposal_requests = [request for request in completed.requests if request.stage == "propose"]
        assert {request.scope_id.removeprefix("position:"): request.frame_hash
                for request in proposal_requests} == original_frames
        new_request = next(request for request in proposal_requests if request.scope_id == position_scope(victim))
        assert new_request.projection_version == COMPACT_PROJECTION
        assert verify_request_evidence(completed)
        assert verify_replay(completed, store.read_events(state.run.id))["ok"]
        assert semantic_projection(store.load(state.run.id)) == semantic_projection(completed)
    finally:
        await runner.shutdown()


async def test_existing_unaccepted_request_keeps_old_projection_on_upgrade(tmp_path, settings, monkeypatch):
    state = synthetic_state(3)
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    victim = "person_2"
    old = VersionedFixture(OLD_PROJECTION, fail_once={victim})
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: old)
    try:
        with pytest.raises(ProviderError) as caught:
            await runner.step(state.run.id)
        assert caught.value.code == "MODEL_TIMEOUT"
        failed = store.load(state.run.id)
        request_before = next(request for request in failed.requests
                              if request.scope_id == position_scope(victim))
        assert request_before.accepted_result_hash is None
        assert request_before.attempts[0].status == "failed"
        accepted_before = {request.id: request.model_dump() for request in failed.requests
                           if request.accepted_result_hash is not None}

        upgraded = VersionedFixture(COMPACT_PROJECTION)
        monkeypatch.setattr("opentale.runner.make_provider", lambda *_: upgraded)
        completed = await runner.step(state.run.id)
        resumed = next(request for request in completed.requests if request.id == request_before.id)

        assert OLD_PROJECTION in upgraded.adapters
        assert ("propose", victim, OLD_PROJECTION) in upgraded.calls
        assert all(version == COMPACT_PROJECTION for stage, _, version in upgraded.calls if stage != "propose")
        assert resumed.frame_hash == request_before.frame_hash
        assert resumed.request_hash == request_before.request_hash
        assert resumed.projection_version == OLD_PROJECTION
        assert resumed.attempts[0] == request_before.attempts[0]
        assert len(resumed.attempts) == 2 and resumed.attempts[1].status == "received"
        assert resumed.attempts[1].projection == request_before.attempts[0].projection
        assert resumed.accepted_result_hash is not None
        assert all(request.model_dump() == accepted_before[request.id]
                   for request in completed.requests if request.id in accepted_before)
        assert completed.run.usage.calls == 8
        assert completed.run.usage.failed_calls == 1
        assert completed.run.usage.unknown_tokens == 0
        assert verify_request_evidence(completed)
        assert verify_replay(completed, store.read_events(state.run.id))["ok"]
    finally:
        await runner.shutdown()
