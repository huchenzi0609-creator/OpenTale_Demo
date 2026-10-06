"""Executable reflection, scoped privacy, simultaneous methods and recovery evidence."""

import asyncio
from itertools import permutations

import pytest
from conftest import ROOT
from opentale.config import Settings
from opentale.domain import Limits, Proposal, Snapshot
from opentale.kernel import KernelError, frame_for, install_batch, reading_frame_for, semantic_projection
from opentale.presets import initialize, read_presets
from opentale.providers import FixtureProvider, ProviderError
from opentale.reflection import (
    ablate_method,
    capture_choice,
    install_feedback,
    objects_for,
    position_scope,
    reading_scope,
    reflection_frame,
)
from opentale.reflection_types import AdoptionDraft, MethodDraft, MethodRecord, ReflectionProposal
from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store


@pytest.fixture
def reflexive():
    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    return initialize(preset, "fixture", Limits(max_calls=200, max_windows=12), "reflexive")


def public_inputs(state, scope):
    return [obj["id"] for obj in objects_for(state, scope).values()
            if obj["kind"] == "content" and obj.get("role") != "主体背景"]


def install_method(state, scope, *, inputs=None, order="listed", limit=16, revises=None, target=None):
    frame = reflection_frame(state, scope, [])
    proposal = ReflectionProposal(
        methods=[MethodDraft(title="并置两种经验", instruction="沿二者的相互作用继续。",
                             inputs=inputs or public_inputs(state, scope)[:2], order=order,
                             limit=limit, revises=revises)],
        adoptions=[AdoptionDraft(scope_id=target or scope, method_id="draft:0")],
    )
    install_feedback(state, [(scope, proposal, frame)])
    return next(m for m in state.reflection.methods
                if m.id in state.reflection.adoptions[target or scope] and m.revises == revises)


def test_method_order_and_limit_change_actual_full_materials(reflexive):
    scope = position_scope("writer")
    inputs = public_inputs(reflexive, scope)[:3]
    original = install_method(reflexive, scope, inputs=inputs)
    before = frame_for(reflexive, "writer")
    revision = install_method(reflexive, scope, inputs=[original.id], order="reverse", limit=1,
                              revises=original.id)
    after = frame_for(reflexive, "writer")
    assert after.reflection.selected_inputs == [inputs[-1]]
    assert after.condition_fingerprint != before.condition_fingerprint
    assert after.seed_text == after.reflection.materials[0].text
    assert after.recent_surface == []
    assert original in reflexive.reflection.methods and revision.revises == original.id


def test_equivalent_methods_do_not_fake_execution_effect(reflexive):
    scope = position_scope("writer")
    first = install_method(reflexive, scope)
    second = install_method(reflexive, scope, revises=first.id)
    before = reflection_frame(reflexive, scope, [])
    clone = reflexive.model_copy(deep=True)
    clone.reflection.adoptions[scope] = [first.id]
    after = reflection_frame(clone, scope, [])
    assert before.input_fingerprint == after.input_fingerprint
    assert second.id != first.id


def test_revision_adoption_is_scoped_and_leaves_other_scope_unchanged(reflexive):
    scope = position_scope("writer")
    original = install_method(reflexive, scope)
    other = position_scope("reader")
    install_feedback(reflexive, [(scope, ReflectionProposal(adoptions=[
        AdoptionDraft(scope_id=other, method_id=original.id),
    ]), reflection_frame(reflexive, scope, []))])
    revision = install_method(reflexive, scope, order="reverse", revises=original.id)
    assert reflexive.reflection.adoptions[scope] == [revision.id]
    assert reflexive.reflection.adoptions[other] == [original.id]


def test_private_profile_cannot_be_exported_via_method(reflexive):
    scope = position_scope("writer")
    private = next(obj["id"] for obj in objects_for(reflexive, scope).values()
                   if obj.get("role") == "主体背景")
    before = reflexive.model_dump()
    with pytest.raises(KernelError, match="不可见"):
        install_method(reflexive, scope, inputs=[private], target=position_scope("reader"))
    assert reflexive.model_dump() == before
    assert reflexive.positions[0].seed not in str(objects_for(reflexive, reading_scope("main")))


def test_choice_reuses_frozen_materials_instead_of_mutated_live_use(reflexive):
    scope = position_scope("writer")
    inputs = public_inputs(reflexive, scope)[:1]
    chosen = reflection_frame(reflexive, scope, inputs)
    choice = capture_choice(reflexive, scope, "propose", chosen)
    old_text = choice.materials[0].text
    use = next(use for use in reflexive.uses if use.id == inputs[0])
    use.bindings[0].value = "后来，这段关系已经发生变化。"
    install_method(reflexive, scope, inputs=[choice.id])
    actual = frame_for(reflexive, "writer").reflection.materials
    assert actual[0].text == old_text
    assert choice.materials[0].text == old_text


def test_candidate_previews_preserve_full_executed_material(reflexive):
    scope = position_scope("writer")
    use = next(use for use in reflexive.uses if use.position_id == "writer" and use.role != "主体背景")
    use.bindings[0].value = "长经历" * 1500
    frame = reflection_frame(reflexive, scope, [use.id])
    row = next(row for row in frame.objects if row["id"] == use.id)
    assert row["preview_truncated"] and len(row["text"]) == 800
    assert len(frame.materials[0].text) > 4000


def test_simultaneous_methods_have_no_last_arrival_winner(reflexive):
    proposals = []
    for p in reflexive.positions:
        scope = position_scope(p.id)
        proposals.append(Proposal(
            position_id=p.id, based_on_revision=0, text=f"{p.name}继续生活。", operations=[],
            reflection=ReflectionProposal(
                methods=[MethodDraft(title=p.name, inputs=public_inputs(reflexive, scope)[:1])],
                adoptions=[AdoptionDraft(scope_id=position_scope("writer"), method_id="draft:0")],
            ),
        ))
    expected = semantic_projection(install_batch(reflexive, proposals, 0))
    for ordering in permutations(proposals):
        assert semantic_projection(install_batch(reflexive, list(ordering), 0)) == expected
    assert len(expected["reflection"]["adoptions"][position_scope("writer")]) == 3
    assert reflexive.run.revision == 0 and reflexive.reflection.methods == []


def test_reflexive_allows_quiet_continuation_but_classic_requires_peer_effect(reflexive, snapshot):
    def drafts(state):
        return [Proposal(position_id=p.id, based_on_revision=0, text=f"{p.name}安静地坐了一会儿。",
                         operations=[]) for p in state.positions]
    installed = install_batch(reflexive, drafts(reflexive), 0)
    assert installed.run.revision == 1
    assert any(obj["id"].startswith("expr-1-") for obj in frame_for(installed, "writer").reflection.objects)
    with pytest.raises(KernelError, match="另一位置"):
        install_batch(snapshot, drafts(snapshot), 0)


def test_invalid_reference_is_atomic_and_cyclic_execution_is_rejected(reflexive):
    scope = position_scope("writer")
    before = reflexive.model_dump()
    with pytest.raises(KernelError, match="本次可见"):
        install_method(reflexive, scope, inputs=["invented-reference"])
    assert reflexive.model_dump() == before
    from opentale.reflection import _execute

    with pytest.raises(KernelError, match="循环"):
        _execute({"self": {"kind": "method", "inputs": ["self"], "instruction": "", "order": "listed",
                           "limit": None}}, ["self"])


def test_deep_method_chain_has_no_step_or_python_recursion_ceiling(reflexive):
    scope = position_scope("writer")
    inputs = public_inputs(reflexive, scope)[:1]
    ref = inputs[0]
    for index in range(1100):
        method = MethodRecord(id=f"deep-{index}", title="继续组合", instruction="", inputs=[ref],
                              limit=None, created_revision=index, source_scope=scope)
        reflexive.reflection.methods.append(method)
        ref = method.id
    reflexive.reflection.adoptions[scope] = [ref]
    frame = reflection_frame(reflexive, scope, [])
    assert frame.selected_inputs == inputs
    assert len(frame.objects) > 256


def test_material_count_and_method_adoption_have_no_budget_ceiling(reflexive):
    scope = position_scope("writer")
    public = public_inputs(reflexive, scope)[0]
    use = next(use for use in reflexive.uses if use.id == public)
    use.bindings[0].value = "完整经历" * 600
    frame = reflection_frame(reflexive, scope, [])
    draft = MethodDraft(title="共同经历", inputs=[public] * 70, limit=None)
    proposal = ReflectionProposal(
        methods=[draft] * 20,
        adoptions=[AdoptionDraft(scope_id=scope, method_id=f"draft:{index}") for index in range(20)],
    )
    install_feedback(reflexive, [(scope, proposal, frame)])
    result = reflection_frame(reflexive, scope, [])
    assert len(reflexive.reflection.adoptions[scope]) == 20
    assert len(result.materials) == 1400
    assert sum(len(material.text) for material in result.materials) > 120000


@pytest.mark.parametrize("mode,version", [("classic", 2), ("reflexive", 3)])
def test_schema_version_roundtrip_and_modes_do_not_silently_convert(mode, version):
    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    state = initialize(preset, "fixture", Limits(), mode)
    assert state.schema_version == version and state.run.mode == mode
    assert Snapshot.model_validate_json(state.model_dump_json()).model_dump() == state.model_dump()


async def test_reflexive_runtime_feedback_replay_and_detached_ablation(reflexive, tmp_path):
    store = Store(tmp_path)
    store.save(reflexive)
    runner = Runner(store, Settings())
    for _ in range(4):
        state = await runner.step(reflexive.run.id)
        assert verify_replay(state, store.read_events(state.run.id))["ok"]
    assert len(state.reflection.choices) == 4 * 7
    assert any(method.revises for method in state.reflection.methods)
    reader_methods = {m.id for m in state.reflection.methods if m.source_scope.startswith("reading:")}
    assert any(reader_methods & set(ids) for scope, ids in state.reflection.adoptions.items()
               if scope.startswith("position:"))
    before = state.model_dump()
    scope, methods = next((scope, methods) for scope, methods in state.reflection.adoptions.items() if methods)
    result = ablate_method(state, scope, methods[0])
    assert result["structural_effect"]
    assert state.model_dump() == before
    assert store.load(state.run.id).model_dump() == before


async def test_realization_failure_resumes_frozen_inputs_without_repeating_accepted_work(
    reflexive, tmp_path, monkeypatch,
):
    store = Store(tmp_path)
    store.save(reflexive)
    runner = Runner(store, Settings())
    fixture = FixtureProvider()
    original = fixture.realize
    failures = 0

    async def fails_once(frame, **kwargs):
        nonlocal failures
        if frame.position_id == "writer" and failures == 0:
            failures += 1
            raise ProviderError("PROVIDER_TIMEOUT", "离线注入中断", True)
        await asyncio.sleep(0)
        return await original(frame, **kwargs)

    fixture.realize = fails_once
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: fixture)
    with pytest.raises(ProviderError):
        await runner.step(reflexive.run.id)
    pending = store.load(reflexive.run.id)
    assert pending.pending.committed and len(pending.pending.realizations) == 2
    saved_frames = pending.pending.realization_frames
    store.recover()
    completed = await Runner(store, Settings()).step(reflexive.run.id)
    assert completed.run.usage.calls == 8 and completed.pending is None
    assert verify_replay(completed, store.read_events(completed.run.id))["ok"]
    actual = next(c for c in completed.reflection.choices if c.scope_id == position_scope("writer")
                  and c.stage == "realize")
    assert actual.input_fingerprint == saved_frames["writer"].reflection.input_fingerprint


async def test_reading_failure_then_new_reading_freezes_and_applies_both_feedbacks(
    reflexive, tmp_path, monkeypatch,
):
    store = Store(tmp_path)
    store.save(reflexive)
    runner = Runner(store, Settings())
    fixture = FixtureProvider()
    original = fixture.present
    failures = 0

    async def fails_once(frame, **kwargs):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise ProviderError("PROVIDER_TIMEOUT", "离线注入阅读中断", True)
        return await original(frame, **kwargs)

    fixture.present = fails_once
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: fixture)
    with pytest.raises(ProviderError):
        await runner.step(reflexive.run.id)
    created = runner.add_reading(reflexive.run.id, "另一种进入", "reader")
    other_id = created.reading_tracks[-1].id
    completed = await runner.step(reflexive.run.id)
    assert len(completed.reading_tracks[-1].story) == 1
    assert reading_scope(other_id) in completed.reflection.adoptions
    assert verify_replay(completed, store.read_events(completed.run.id))["ok"]
    assert all(use["role"] != "主体背景" for c in reading_frame_for(completed).candidates
               for use in c["active_uses"])


async def test_replay_detects_changed_actual_selection(reflexive, tmp_path):
    store = Store(tmp_path)
    store.save(reflexive)
    state = await Runner(store, Settings()).step(reflexive.run.id)
    events = store.read_events(state.run.id)
    resumed = next(event for event in events if event.type == "continuation_resumed")
    resumed.data["choice"]["selected"] = []
    assert not verify_replay(state, events)["ok"]


async def test_release_at_recovery_point_preserves_frozen_work_and_replays(reflexive, tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save(reflexive)
    runner = Runner(store, Settings())
    fixture = FixtureProvider()
    original = fixture.realize
    failures = 0

    async def fails_once(frame, **kwargs):
        nonlocal failures
        if frame.position_id == "writer" and failures == 0:
            failures += 1
            raise ProviderError("PROVIDER_TIMEOUT", "离线注入中断", True)
        return await original(frame, **kwargs)

    fixture.realize = fails_once
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: fixture)
    with pytest.raises(ProviderError):
        await runner.step(reflexive.run.id)
    failed = store.load(reflexive.run.id)
    scope = position_scope("writer")
    method_id = failed.reflection.adoptions[scope][0]
    frozen = failed.pending.realization_frames["writer"].model_dump()
    released = runner.release_method(reflexive.run.id, scope, method_id)
    assert released.pending.realization_frames["writer"].model_dump() == frozen
    assert method_id not in released.reflection.adoptions[scope]
    completed = await runner.step(reflexive.run.id)
    assert verify_replay(completed, store.read_events(completed.run.id))["ok"]
    trace = next(c for c in completed.reflection.choices if c.scope_id == scope and c.stage == "realize")
    assert trace.methods == frozen["reflection"]["active_methods"]
