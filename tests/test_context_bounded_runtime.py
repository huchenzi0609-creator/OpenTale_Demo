"""Adversarial bounded-input and completed-work recovery checks, entirely offline."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from opentale.config import Settings
from opentale.kernel import fingerprint, semantic_projection
from opentale.policy_types import View, ViewGroup
from opentale.providers import DeepSeekProvider, FixtureProvider, ModelResult, ProviderError
from opentale.reflection_types import Material
from opentale.replay import verify_replay
from opentale.request_evidence import verify_request_evidence
from opentale.runner import Runner
from opentale.storage import Store
from test_context_provider import repeated_frame
from test_context_runtime import synthetic_state
from test_reflexive_v2_provider import v2_frame

OLD_CONTEXT_VERSION = "provider-json-v2-context"
BOUNDED_VERSION = "provider-json-v3-bounded"


def test_v2_context_adapter_preserves_pre_upgrade_request_hash():
    # Captured before v3 implementation using only this repository's synthetic
    # fixture. An old frozen request must retain its exact prompts and schema,
    # not merely keep the old version label after projection code changes.
    provider = DeepSeekProvider(Settings(context_target_chars=96_000)).with_projection_version(OLD_CONTEXT_VERSION)
    projection = provider.request_projection(repeated_frame(), "propose")
    assert fingerprint(projection) == "176330fb73f7eda4590a8daf2971da0d60da91d8ee8dd66d536c907ed9a80025"


def adversarial_frame(case: str):
    frame = v2_frame(reading=True)
    reflection = frame.reflection
    if case == "medium_sentences":
        reflection.materials = [Material(
            id=f"material-{index}", role="经历",
            text=f"{index:05d}" + "甲" * 495 + "。" + "乙" * 499 + "。",
        ) for index in range(600)]
        reflection.view = View(material_ids=[material.id for material in reflection.materials], groups=[
            ViewGroup(id="shared", label="彼此留下的经历",
                      material_ids=[material.id for material in reflection.materials]),
        ])
    elif case == "mandatory_groups":
        reflection.view = View(material_ids=[material.id for material in reflection.materials], groups=[
            ViewGroup(id=f"group-{index}", label="这个位置的组织关系") for index in range(10_000)
        ])
    elif case == "short_history":
        frame.previous_segments = [
            {"id": f"segment-{index}", "revision": index, "focus_position_id": "person-a",
             "expression_ids": [], "reinterprets": [], "text": chr(0x4e00 + index)}
            for index in range(6000)
        ]
    elif case == "candidate_edges":
        first_ids = [f"candidate-{index}" for index in range(100)]
        reflection.objects.extend({
            "id": f"candidate-{index}", "kind": "effect", "text": "此前组织产生的变化",
            "version": f"version-{index}", "selected_added": first_ids,
        } for index in range(4000))
    else:
        raise AssertionError("Unknown synthetic case")
    return frame


@pytest.mark.parametrize("case", ["medium_sentences", "mandatory_groups", "short_history", "candidate_edges"])
def test_adversarial_present_projection_fits_actual_system_schema_and_user(case):
    from opentale.providers.context_bounded import verify_bounded_sources

    frame = adversarial_frame(case)
    original = frame.model_dump()
    provider = DeepSeekProvider(Settings(context_target_chars=96_000))
    projection = provider.request_projection(frame, "present")

    assert provider.projection_version == BOUNDED_VERSION
    assert sum(len(message["content"]) for message in projection["messages"]) < 512_000
    data = json.loads(next(message["content"] for message in projection["messages"]
                           if message["role"] == "user"))
    manifest = data["context_compaction"]
    assert manifest["projection_version"] == BOUNDED_VERSION
    assert manifest["algorithm"] == "source-pages-v1"
    assert manifest["structure_excerpted"] or manifest["lossy"]
    if case != "medium_sentences":
        assert manifest["structure_excerpted"] is True
        assert any(page["omitted_count"] > 0 for page in manifest["pages"])
    assert manifest["material_occurrences"] == len(frame.reflection.materials)
    assert manifest["candidate_objects"] == len(frame.reflection.objects)
    assert manifest["history_segments"] == len(frame.previous_segments)
    # A short current organizing instruction is critical input, even when the
    # surrounding catalog is large; paging must not silently blank it out.
    assert data["reflection"]["instruction"] == frame.reflection.instruction
    for page in manifest["pages"]:
        assert page["omitted_count"] >= 0
        assert page["total"] >= page["omitted_count"]
        assert len(page["sha256"]) == 64
    assert frame.model_dump() == original
    assert verify_bounded_sources(frame, projection)


class ReadingUpgradeFixture(FixtureProvider):
    """Stop at reading preflight after all sixteen actor jobs have been accepted."""

    def __init__(self, version: str, *, refuse_reading=False, calls=None):
        self.projection_version = version
        self.refuse_reading = refuse_reading
        self.calls = calls if calls is not None else []

    def with_projection_version(self, version):
        return ReadingUpgradeFixture(version, calls=self.calls)

    def request_projection(self, frame, method, *, repair=None):
        if method == "present" and self.refuse_reading:
            raise ProviderError("CONTEXT_TOO_LARGE", "合成阅读结构在传输前被拒绝。")
        return {**super().request_projection(frame, method, repair=repair),
                "synthetic_projection_version": self.projection_version}

    async def propose(self, frame, **kwargs):
        self.calls.append(("propose", frame.position_id, self.projection_version))
        result = await super().propose(frame, **kwargs)
        return ModelResult(result.value, input_tokens=11, output_tokens=7)

    async def realize(self, frame, **kwargs):
        self.calls.append(("realize", frame.position_id, self.projection_version))
        result = await super().realize(frame, **kwargs)
        return ModelResult(result.value, input_tokens=11, output_tokens=7)

    async def present(self, frame, **kwargs):
        self.calls.append(("present", frame.start_position_id, self.projection_version))
        result = await super().present(frame, **kwargs)
        return ModelResult(result.value, input_tokens=5, output_tokens=3)


async def test_two_reading_preflight_failures_resume_without_repeating_sixteen_accepted_actor_jobs(
    tmp_path, settings, monkeypatch,
):
    state = synthetic_state(8)
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    state = runner.add_reading(state.run.id, "第二入口", "person_7")
    old = ReadingUpgradeFixture(OLD_CONTEXT_VERSION, refuse_reading=True)
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: old)
    try:
        with pytest.raises(ProviderError) as caught:
            await runner.step(state.run.id)
        assert caught.value.code == "CONTEXT_TOO_LARGE"
        failed = store.load(state.run.id)
        assert failed.pending is not None and failed.pending.committed
        assert failed.run.phase == "presenting"
        assert len(failed.pending.proposals) == len(failed.pending.realizations) == 8
        assert len(failed.pending.reading_frames) == 2 and not failed.pending.presentations
        assert len(failed.requests) == failed.run.usage.calls == 16
        assert all(request.accepted_result_hash is not None for request in failed.requests)
        assert all(request.stage in {"propose", "realize"} for request in failed.requests)
        assert failed.run.usage.failed_calls == 0
        assert failed.run.usage.input_tokens == 16 * 11
        assert failed.run.usage.output_tokens == 16 * 7
        assert failed.run.usage.unknown_tokens == failed.run.usage.reserved_tokens == 0
        accepted_before = {request.id: request.model_dump() for request in failed.requests}
        frames_before = {key: fingerprint(frame.model_dump())
                         for key, frame in failed.pending.reading_frames.items()}
        actor_events = [event.model_dump() for event in failed.events
                        if event.type in {"proposal_ready", "continuation_resumed"}]

        # A second preflight refusal still must not re-run paid or accepted actor work.
        with pytest.raises(ProviderError):
            await runner.step(state.run.id)
        refused_again = store.load(state.run.id)
        assert refused_again.run.usage == failed.run.usage
        assert len(old.calls) == 16

        bounded = ReadingUpgradeFixture(BOUNDED_VERSION)
        monkeypatch.setattr("opentale.runner.make_provider", lambda *_: bounded)
        completed = await runner.step(state.run.id)

        assert completed.pending is None and completed.run.revision == 1
        assert Counter(stage for stage, _, _ in bounded.calls) == {"present": 2}
        assert len(completed.requests) == completed.run.usage.calls == 18
        assert completed.run.usage.failed_calls == 0
        assert completed.run.usage.input_tokens == failed.run.usage.input_tokens + 2 * 5
        assert completed.run.usage.output_tokens == failed.run.usage.output_tokens + 2 * 3
        assert completed.run.usage.unknown_tokens == completed.run.usage.reserved_tokens == 0
        assert all(request.model_dump() == accepted_before[request.id]
                   for request in completed.requests if request.id in accepted_before)
        assert [event.model_dump() for event in completed.events
                if event.type in {"proposal_ready", "continuation_resumed"}] == actor_events
        reads = [request for request in completed.requests if request.stage == "present"]
        assert {request.scope_id.removeprefix("reading:"): request.frame_hash for request in reads} == frames_before
        assert all(request.projection_version == BOUNDED_VERSION for request in reads)
        assert all(len(track.story) == 1 for track in completed.reading_tracks)
        assert verify_request_evidence(completed)
        assert verify_replay(completed, store.read_events(state.run.id))["ok"]
        assert semantic_projection(store.load(state.run.id)) == semantic_projection(completed)
    finally:
        await runner.shutdown()
