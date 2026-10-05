from itertools import permutations

import pytest
from opentale.kernel import KernelError, frame_for, install_batch, without_source
from opentale.providers import make_provider


async def proposals(snapshot, settings):
    fixture = make_provider("fixture", settings)
    return [(await fixture.propose(frame_for(snapshot, p.id))).value for p in snapshot.positions]


async def test_joint_batch_is_order_independent_and_source_unchanged(snapshot, settings):
    original = snapshot.model_dump()
    draft = await proposals(snapshot, settings)
    installed = install_batch(snapshot, draft, 0)
    assert all(
        install_batch(snapshot, list(p), 0).model_dump() == installed.model_dump()
        for p in permutations(draft)
    )
    assert snapshot.model_dump() == original
    assert len(installed.audit["joint_targets"]) == 3
    assert all(c["changed"] for c in installed.audit["condition_changes"])


async def test_stale_rejection_is_atomic(snapshot, settings):
    draft = await proposals(snapshot, settings)
    installed = install_batch(snapshot, draft, 0)
    before = installed.model_dump()
    with pytest.raises(KernelError, match="改变"):
        install_batch(installed, draft, 0)
    assert installed.model_dump() == before


@pytest.mark.parametrize("kind", ["anchor", "reference", "self_only", "missing", "duplicate"])
async def test_invalid_rewrites_rejected_before_mutation(snapshot, settings, kind):
    draft = await proposals(snapshot, settings)
    before = snapshot.model_dump()
    if kind == "anchor":
        draft[0].operations[0].anchor = "完全不在表达中的原文"
    elif kind == "reference":
        draft[0].operations[0].expression_id = "missing"
    elif kind == "self_only":
        for op in draft[0].operations:
            op.target_position_id = draft[0].position_id
    elif kind == "missing":
        draft.pop()
    else:
        draft[1] = draft[0]
    with pytest.raises(KernelError):
        install_batch(snapshot, draft, 0)
    assert snapshot.model_dump() == before


async def test_conflicting_bindings_survive_and_ablation_changes_realizer(snapshot, settings):
    draft = await proposals(snapshot, settings)
    target = snapshot.positions[0].id
    for index, proposal in enumerate(draft[1:]):
        op = next(o for o in proposal.operations if o.target_position_id == target)
        op.value = ["屋里无人。", "屋里还有人。"][index]
        op.anchor = op.value
        proposal.text += op.value
    installed = install_batch(snapshot, draft, 0)
    use = next(u for u in installed.uses if u.position_id == target and u.expression_id == "e-letter")
    assert {b.value for b in use.bindings} == {"屋里无人。", "屋里还有人。"}
    assert len({b.source_position_id for b in use.bindings}) == 2
    changed, use_ids = without_source(installed, target, draft[1].position_id)
    assert use_ids == [use.id]
    fixture = make_provider("fixture", settings)
    assert (await fixture.realize(frame_for(installed, target))).value != (
        await fixture.realize(frame_for(changed, target))
    ).value


async def test_profile_is_revisable_and_actor_is_source_blind(snapshot, settings):
    draft = await proposals(snapshot, settings)
    op = draft[1].operations[0]
    op.target_position_id = snapshot.positions[0].id
    op.expression_id = f"seed-{op.target_position_id}"
    op.role = "主体背景"
    op.value = "她从这一份全新的过去形成自己。"
    op.anchor = op.value
    draft[1].text += op.value
    installed = install_batch(snapshot, draft, 0)
    frame = frame_for(installed, op.target_position_id)
    profile = next(u for u in frame.active_uses if u["role"] == "主体背景")
    assert profile["text"] == op.value
    assert snapshot.positions[0].seed not in frame.model_dump_json()
    for use in frame.active_uses:
        assert "source_position_id" not in use and "source_expression_id" not in use


async def test_noop_peer_batch_cannot_fake_effective_dependence(snapshot, settings):
    draft = await proposals(snapshot, settings)
    original = next(e.text for e in snapshot.expressions if e.id == "e-letter")
    for proposal in draft:
        proposal.text = original
        for op in proposal.operations:
            op.value = op.anchor = original
    with pytest.raises(KernelError, match="实际条件"):
        install_batch(snapshot, draft, 0)


async def test_single_input_relay_cannot_be_called_joint_install(snapshot, settings):
    draft = await proposals(snapshot, settings)
    targets = ["keeper", "reader", "writer"]
    for proposal, target in zip(draft, targets, strict=True):
        proposal.operations = [next(o for o in proposal.operations if o.target_position_id == target)]
    with pytest.raises(KernelError) as caught:
        install_batch(snapshot, draft, 0)
    assert caught.value.code == "JOINT_DEPENDENCY_MISSING"
