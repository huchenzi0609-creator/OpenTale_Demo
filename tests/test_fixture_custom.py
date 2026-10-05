"""Offline preset tests verify executable coupling, not literary model quality."""

from __future__ import annotations

import pytest
from opentale.domain import Binding, Expression, Limits, ReadingFrame, Use
from opentale.kernel import frame_for, install_batch, install_presentation, use_id, without_source
from opentale.presets import initialize
from opentale.providers import FixtureProvider, ProviderError
from opentale.runner import Runner


def custom_snapshot(count: int = 3, *, expression_id: str = "river-origin", text: str = "河水涨过台阶。"):
    return initialize(
        {
            "id": "custom-river",
            "name": "河边的几处生活",
            "positions": [
                {"id": f"person_{index}", "name": f"河边人{index}", "seed": f"她记得第{index}次涨水。"}
                for index in range(count)
            ],
            "expressions": [{"id": expression_id, "text": text}],
        },
        "fixture",
        Limits(max_calls=100, max_windows=5),
    )


@pytest.mark.parametrize("count", range(3, 9))
async def test_custom_preset_three_windows_keep_all_peers_joint_and_order_independent(count, settings, tmp_path):
    fixture = FixtureProvider()
    state = custom_snapshot(count)
    for revision in range(3):
        proposals = [(await fixture.propose(frame_for(state, p.id))).value for p in state.positions]
        for proposal in proposals:
            operations = proposal.operations
            assert len(operations) == count - 1
            assert all(op.expression_id == "river-origin" for op in operations)
            assert all(op.role == "共同生活" for op in operations)
            assert all(op.value == op.anchor and op.anchor in proposal.text for op in operations)
            assert "信" not in proposal.text and "门" not in proposal.text
        installed = install_batch(state, proposals, revision)
        backwards = install_batch(state, list(reversed(proposals)), revision)
        assert installed.model_dump() == backwards.model_dump()
        assert len(installed.audit["joint_targets"]) == count
        for target in installed.audit["joint_targets"]:
            assert len(target["sources"]) == count - 1
        assert all(change["changed"] for change in installed.audit["condition_changes"])
        for continuation in installed.continuations:
            continuation.surface = (await fixture.realize(frame_for(installed, continuation.position_id))).value
        reading = Runner(None, settings).reading_frame(installed)
        presentation = (await fixture.present(reading)).value
        assert presentation.expression_ids == ["river-origin"]
        assert "信" not in presentation.text and "门" not in presentation.text
        install_presentation(installed, presentation)
        assert presentation.reinterprets == ([state.story[-1].id] if state.story else [])
        state = installed
    assert len(state.story) == 3


def add_use(state, position_id, expression_id, role, text):
    state.uses.append(
        Use(
            id=use_id(position_id, expression_id, role),
            position_id=position_id,
            expression_id=expression_id,
            role=role,
            bindings=[Binding(
                source_position_id=position_id, source_expression_id=expression_id,
                value=text, anchor=text, revision=0,
            )],
            revision=0,
        )
    )


async def test_selection_matches_role_and_peer_coverage_not_private_letter_alias():
    state = custom_snapshot(5)
    private_id = "e-letter"
    state.expressions.append(Expression(id=private_id, text="一封仅属自己的信。", created_revision=0))
    add_use(state, "person_0", private_id, "主体背景", "一封仅属自己的信。")
    state.expressions.append(Expression(id="a-less-shared", text="雪已经停了。", created_revision=0))
    for position in state.positions[:3]:
        add_use(state, position.id, "a-less-shared", "梦中的雪", "雪已经停了。")
    # Same expression, different roles are not a common writable target interface.
    for index, position in enumerate(state.positions):
        add_use(state, position.id, "a-less-shared", f"各自的含义{index}", "雪已经停了。")
    fixture = FixtureProvider()
    frame = frame_for(state, "person_0")
    proposal = (await fixture.propose(frame)).value
    assert {op.expression_id for op in proposal.operations} == {"river-origin"}
    assert {op.target_position_id for op in proposal.operations} == {f"person_{index}" for index in range(1, 5)}
    assert "那封信" not in proposal.text and "雪" not in proposal.text


async def test_equal_coverage_uses_stable_expression_then_free_role():
    state = custom_snapshot(3, expression_id="z-source")
    state.expressions.append(Expression(id="a-source", text="石头有了裂缝。", created_revision=0))
    for position in state.positions:
        add_use(state, position.id, "a-source", "谁仍在承担这段过去", "石头有了裂缝。")
    fixture = FixtureProvider()
    proposals = [(await fixture.propose(frame_for(state, p.id))).value for p in state.positions]
    assert all(op.expression_id == "a-source" for proposal in proposals for op in proposal.operations)
    assert all(op.role == "谁仍在承担这段过去" for proposal in proposals for op in proposal.operations)
    installed = install_batch(state, proposals, 0)
    assert len(installed.audit["joint_targets"]) == 3


async def test_custom_frame_cannot_fall_back_to_private_letter_when_shared_interface_missing():
    state = custom_snapshot(3, expression_id="e-letter", text="私人收藏的一封信。")
    state.uses = [use for use in state.uses if use.expression_id != "e-letter" or use.position_id == "person_0"]
    with pytest.raises(ProviderError) as caught:
        await FixtureProvider().propose(frame_for(state, "person_0"))
    assert caught.value.code == "FIXTURE_FRAME_INVALID"


@pytest.mark.parametrize("text", ["只有钟声在山谷里回荡。", "她把信心留给明日。", "灯火照着桌面。"])
async def test_generic_carrier_does_not_invent_letter_or_door(text):
    state = custom_snapshot(text=text)
    proposal = (await FixtureProvider().propose(frame_for(state, "person_0"))).value
    assert "那封信" not in proposal.text and "门外" not in proposal.text
    assert text in proposal.text


async def test_every_scoped_binding_is_read_without_prefix_truncation_and_ablation_changes_surface():
    fixture = FixtureProvider()
    state = custom_snapshot(4)
    proposals = [(await fixture.propose(frame_for(state, p.id))).value for p in state.positions]
    installed = install_batch(state, proposals, 0)
    use = next(u for u in installed.uses if u.position_id == "person_0" and u.expression_id == "river-origin")
    for index, binding in enumerate(use.bindings):
        binding.value = "共同内容" * 110 + f"各自尾声{index}"
    baseline = (await fixture.realize(frame_for(installed, "person_0"))).value
    assert all(binding.value in baseline for binding in use.bindings)
    for binding in use.bindings:
        changed, _ = without_source(installed, "person_0", binding.source_position_id)
        alternative = (await fixture.realize(frame_for(changed, "person_0"))).value
        assert alternative != baseline
        assert binding.value not in alternative


async def test_present_reads_open_roles_focus_visits_and_custom_shared_ids():
    fixture = FixtureProvider()
    candidates = [
        {
            "position_id": f"person_{index}", "name": f"河边人{index}", "text": f"她想起第{index}次涨水。",
            "active_uses": [
                {"expression_id": "river-origin", "role": "共同生活", "values": ["河水又涨过台阶。"]},
                {"expression_id": f"private-{index}", "role": "主体背景", "values": ["私人过去。"]},
            ],
        }
        for index in range(3)
    ]
    frame = ReadingFrame(
        revision=50, candidates=candidates, current_focus="person_0",
        previous_segments=[{
            "id": "earlier", "focus_position_id": "person_0", "expression_ids": ["river-origin"],
        }],
        open_relations=[{
            "position_id": "person_2", "expression_id": "river-origin", "role": "共同生活",
            "position_ids": ["person_0", "person_1", "person_2"],
        }],
    )
    presentation = (await fixture.present(frame)).value
    assert presentation.focus_position_id == "person_2"
    assert presentation.expression_ids == ["river-origin"]
    assert presentation.reinterprets == ["earlier"]
    assert "她想起第2次涨水。" in presentation.text
    later_revision = frame.model_copy(update={"revision": 51})
    assert (await fixture.present(later_revision)).value == presentation
    moved_relation = frame.model_copy(deep=True)
    moved_relation.open_relations[0]["position_id"] = "person_1"
    assert (await fixture.present(moved_relation)).value.focus_position_id == "person_1"
    no_relations = frame.model_copy(update={"open_relations": []})
    assert (await fixture.present(no_relations)).value.focus_position_id == "person_1"


async def test_present_rejects_collection_of_only_private_expressions():
    frame = ReadingFrame(
        revision=0, candidates=[
            {"position_id": "a", "text": "她想起过去。", "active_uses": [{"expression_id": "private-a"}]},
            {"position_id": "b", "text": "她想到明日。", "active_uses": [{"expression_id": "private-b"}]},
        ], previous_segments=[], open_relations=[],
    )
    with pytest.raises(ProviderError) as caught:
        await FixtureProvider().present(frame)
    assert caught.value.code == "FIXTURE_FRAME_INVALID"


async def test_present_multiple_shared_carriers_enter_surface_without_private_provenance():
    candidates = [
        {
            "position_id": position, "text": "她停在河边。",
            "active_uses": [
                {"expression_id": "river", "values": ["河水涨过台阶。"]},
                {"expression_id": "stone", "values": ["石头有了裂缝。"]},
                {"expression_id": f"private-{position}", "values": ["不可见的私人背景。"]},
            ],
            "source_position_id": "technical-creator-not-prose",
        }
        for position in ("a", "b", "c")
    ]
    frame = ReadingFrame(revision=3, candidates=candidates, previous_segments=[], open_relations=[])
    result = (await FixtureProvider().present(frame)).value
    assert result.expression_ids == ["river", "stone"]
    assert "河水涨过台阶。" in result.text and "石头有了裂缝。" in result.text
    assert "私人背景" not in result.text and "technical-creator" not in result.text
