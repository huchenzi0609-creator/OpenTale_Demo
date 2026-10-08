"""V2 programs alter real views while preserving permissions, versions and legacy behavior."""

import pytest
from conftest import ROOT
from opentale.domain import Expression, Limits
from opentale.kernel import KernelError, frame_for
from opentale.policy_types import (
    ApplyStep,
    GroupStep,
    InspectStep,
    MethodPatch,
    ObjectRef,
    PolicyProgram,
    QueryStep,
    ReadStep,
    RelateStep,
    ReviseStep,
    ViewRelation,
)
from opentale.presets import initialize, read_presets
from opentale.reflection import (
    capture_choice,
    install_feedback,
    objects_for,
    position_scope,
    reflection_frame,
)
from opentale.reflection_types import AdoptionDraft, MethodDraft, ReflectionProposal, ResponseDraft
from pydantic import ValidationError


@pytest.fixture
def world():
    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    return initialize(preset, "fixture", Limits(max_calls=None, max_windows=None), "reflexive_v2")


def public_ids(world, scope="position:writer"):
    return [identifier for identifier, row in objects_for(world, scope).items()
            if row["kind"] == "content" and not row.get("private_scope")]


def program(*steps, output=None):
    return PolicyProgram(steps=list(steps), output=output or steps[-1].id)


def read(identifier, id="read", **kwargs):
    return ReadStep(id=id, refs=[ObjectRef(object_id=identifier, **kwargs)])


def install(world, draft, *, scope="position:writer", target=None, responses=None):
    frame = reflection_frame(world, scope, [])
    install_feedback(world, [(scope, ReflectionProposal(methods=[draft], responses=responses or [], adoptions=[
        AdoptionDraft(scope_id=target or scope, method_id="draft:0"),
    ]), frame)])
    return next(method for method in world.reflection.methods
                if method.id in world.reflection.adoptions[target or scope] and method.title == draft.title)


def test_inspect_revision_application_changes_real_input_and_installs_revision(world):
    identifiers = public_ids(world)[:3]
    original = install(world, MethodDraft(title="原组织", inputs=identifiers, limit=None))
    before = frame_for(world, "writer").reflection
    policy = program(
        InspectStep(id="definition", ref=ObjectRef(object_id=original.id)),
        ReviseStep(id="new_definition", input="definition", patch=MethodPatch(order="reverse", limit=1)),
        ApplyStep(id="apply", method="new_definition"),
    )
    replacement = install(world, MethodDraft(title="检查再改版", language_version=2, program=policy,
                                             revises=original.id))
    after = frame_for(world, "writer").reflection
    assert after.selected_inputs == [identifiers[-1]]
    assert after.input_fingerprint != before.input_fingerprint
    generated = [method for method in world.reflection.methods if method.id.startswith("derived-")]
    assert len(generated) == 1
    assert generated[0].revises == original.id and generated[0].order == "reverse"
    assert world.reflection.adoptions["position:writer"] == [replacement.id]
    assert world.policy_revision == 2


def test_nested_groups_and_relations_survive_selection(world):
    first, second = public_ids(world)[:2]
    draft = MethodDraft(title="关系", language_version=2, program=program(
        read(first, "one"), read(second, "two"),
        GroupStep(id="cluster", inputs=["one"], label="回望"),
        RelateStep(id="relation", inputs=["cluster", "two"], relations=[
            ViewRelation(source="cluster", target="two", label="重新解释"),
        ]),
        GroupStep(id="whole", inputs=["relation"], label="一种读法"),
    ))
    install(world, draft)
    frame = frame_for(world, "writer").reflection
    assert frame.view.groups[0].label == "一种读法"
    assert frame.view.groups[0].children[0].children[0].label == "回望"
    assert frame.view.relations[0].label == "重新解释"
    assert frame.selected_inputs == [first, second]


def test_equivalent_apply_wrapper_changes_plan_not_effective_input(world):
    selected = public_ids(world)[0]
    original = install(world, MethodDraft(title="读取", language_version=2, program=program(read(selected))))
    before = frame_for(world, "writer").reflection
    replacement = MethodDraft(title="等价的封装", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)),
        ApplyStep(id="apply", method="inspect"), GroupStep(id="identity", inputs=["apply"]),
    ))
    install(world, replacement)
    after = frame_for(world, "writer").reflection
    assert after.input_fingerprint == before.input_fingerprint
    assert after.plan_fingerprint != before.plan_fingerprint


def test_exact_version_and_saved_view_do_not_follow_mutated_live_use(world):
    selected = public_ids(world)[0]
    initial = reflection_frame(world, "position:writer", [selected])
    choice = capture_choice(world, "position:writer", "propose", initial)
    old_text = initial.materials[0].text
    version = next(row["version"] for row in initial.objects if row["id"] == selected)
    pinned = install(world, MethodDraft(title="精确版本", language_version=2,
                                       program=program(read(selected, mode="version", version=version))))
    use = next(use for use in world.uses if use.id == selected)
    use.bindings[0].value = "后来这段关系已经完全变化。"
    assert frame_for(world, "writer").reflection.materials[0].text == old_text
    assert choice.materials[0].text == old_text
    new = MethodDraft(title="活版本", language_version=2, revises=pinned.id, program=program(read(selected)))
    install(world, new)
    assert frame_for(world, "writer").reflection.materials[0].text != old_text
    assert choice.materials[0].text == old_text
    assert world.reflection.views[0].materials[0].text == old_text


def test_query_finds_old_full_chinese_material_beyond_recent_candidates(world):
    world.expressions.append(Expression(id="old-source", text="开头" * 900 + "潮湿的铜币在这里。",
                                        created_revision=1))
    for index in range(130):
        world.expressions.append(Expression(id=f"distractor-{index}", text="河岸在傍晚变得安静。",
                                            created_revision=2))
    install(world, MethodDraft(title="寻找旧材料", language_version=2, program=program(
        QueryStep(id="find", query="潮湿的铜币", kinds=["content"], limit=1),
    )))
    frame = frame_for(world, "writer").reflection
    assert frame.selected_inputs == ["old-source"]
    assert len(frame.materials[0].text) > 1800
    row = next(row for row in frame.objects if row["id"] == "old-source")
    assert row["preview_truncated"] and row["full_length"] > 1800


def test_query_scope_filter_and_transitive_shared_response_privacy(world):
    scope = position_scope("writer")
    private_id = next(identifier for identifier, row in objects_for(world, scope).items()
                      if row.get("role") == "主体背景")
    frame = reflection_frame(world, scope, [])
    install_feedback(world, [(scope, ReflectionProposal(responses=[ResponseDraft(
        text="这里保留了人物的私有起点。", about=[private_id], visibility="shared",
    )]), frame)])
    response = world.reflection.responses[0]
    assert response.id in objects_for(world, scope)
    assert response.id not in objects_for(world, "position:reader")
    before = world.model_dump()
    with pytest.raises(KernelError, match="不可见"):
        install(world, MethodDraft(title="转交私有回应", language_version=2,
                                   program=program(read(response.id))), target="position:reader")
    assert world.model_dump() == before
    install(world, MethodDraft(title="查找私有起点", language_version=2, program=program(
        QueryStep(id="find", query="这里保留了人物的私有起点", kinds=["response"]),
    )), scope="position:reader")
    assert frame_for(world, "reader").reflection.materials == []


def test_local_response_can_drive_revised_method_and_changed_view(world):
    first, second = public_ids(world)[:2]
    original = install(world, MethodDraft(title="初始关注", language_version=2, program=program(read(first))))
    before = frame_for(world, "writer").reflection
    capture_choice(world, "position:writer", "realize", before)
    frame = reflection_frame(world, "position:reader", [])
    install_feedback(world, [("position:reader", ReflectionProposal(responses=[ResponseDraft(
        text="这份组织让另一段经历获得了重新进入的机会。", about=[original.id], evidence=[second],
        visibility="shared",
    )]), frame)])
    response = world.reflection.responses[0]
    replacement_program = program(read(second, "new_material"), read(response.id, "response"),
                                  RelateStep(id="link", inputs=["new_material", "response"], relations=[
                                      ViewRelation(source="response", target="new_material", label="提出重新进入"),
                                  ]))
    install(world, MethodDraft(title="回应之后", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)),
        ReviseStep(id="revise", input="inspect", patch=MethodPatch(program=replacement_program)),
        ApplyStep(id="apply", method="revise"),
    )))
    world.run.revision += 1
    after = frame_for(world, "writer").reflection
    capture_choice(world, "position:writer", "realize", after)
    assert after.selected_inputs == [second, response.id]
    assert after.input_fingerprint != before.input_fingerprint
    effect = world.reflection.effects[-1]
    assert effect.changed and response.id in effect.response_ids
    assert effect.before_fingerprint == before.input_fingerprint


def test_program_type_errors_are_rejected_before_execution():
    with pytest.raises(ValidationError, match="preceding"):
        program(ApplyStep(id="bad", method="missing"))
    with pytest.raises(ValidationError, match="return a view"):
        program(InspectStep(id="definition", ref=ObjectRef(object_id="some-method")))


def test_revision_patch_presence_survives_method_and_snapshot_roundtrip(world):
    from opentale.domain import Snapshot

    identifiers = public_ids(world)[:2]
    original = install(world, MethodDraft(title="有限方法", inputs=identifiers, limit=1))
    patch = MethodPatch(limit=None, order="reverse")
    assert patch.model_dump() == {"order": "reverse", "limit": None}
    assert MethodPatch.model_validate(patch.model_dump()).model_fields_set == {"order", "limit"}
    install(world, MethodDraft(title="取消选择限额", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)),
        ReviseStep(id="change", input="inspect", patch=patch), ApplyStep(id="apply", method="change"),
    )))
    restored = Snapshot.model_validate_json(world.model_dump_json())
    assert frame_for(restored, "writer").reflection.selected_inputs == list(reversed(identifiers))
    assert frame_for(restored, "writer").reflection.input_fingerprint == frame_for(world, "writer").reflection.input_fingerprint


def test_inspected_definitions_available_without_silently_applying_them(world):
    identifiers = public_ids(world)[:2]
    original = install(world, MethodDraft(title="被检查", inputs=identifiers))
    install(world, MethodDraft(title="仅观察定义", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)), read(identifiers[0]),
    )))
    frame = frame_for(world, "writer").reflection
    assert frame.selected_inputs == [identifiers[0]]
    inspected = next(definition for definition in frame.definitions if definition["operation"] == "inspect")
    assert inspected["inputs"] == identifiers
    assert inspected["object_id"] == original.id


def test_duplicate_view_uses_have_distinct_relation_endpoints(world):
    first, second = public_ids(world)[:2]
    original = install(world, MethodDraft(title="关系视图", language_version=2, program=program(
        read(first, "one"), read(second, "two"),
        RelateStep(id="relate", inputs=["one", "two"], relations=[
            ViewRelation(source="one", target="two", label="相互映照"),
        ]),
    )))
    install(world, MethodDraft(title="两次进入", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)),
        ApplyStep(id="first", method="inspect"), ApplyStep(id="second", method="inspect"),
        GroupStep(id="both", inputs=["first", "second"], label="重读"),
    )))
    frame = frame_for(world, "writer").reflection
    assert len(frame.view.relations) == 2
    assert frame.view.relations[0].source != frame.view.relations[1].source
    all_ids = []
    pending = list(frame.view.groups)
    while pending:
        group = pending.pop()
        all_ids.append(group.id)
        pending.extend(group.children)
    assert len(all_ids) == len(set(all_ids))
    assert all(edge.source in all_ids and edge.target in all_ids for edge in frame.view.relations)


def test_empty_feedback_does_not_change_policy_revision(world):
    before = world.policy_revision
    frame = frame_for(world, "writer").reflection
    install_feedback(world, [("position:writer", ReflectionProposal(), frame)])
    assert world.policy_revision == before


def test_private_inspection_cannot_escape_through_public_output_view_or_revision(world):
    scope = "position:writer"
    private = next(identifier for identifier, row in objects_for(world, scope).items()
                   if row.get("role") == "主体背景")
    public = public_ids(world)[0]
    original = install(world, MethodDraft(title="私有方法", inputs=[private], instruction="私有组织说明"))
    install(world, MethodDraft(title="仍有私有来路", language_version=2, revises=original.id, program=program(
        InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)),
        ReviseStep(id="revise", input="inspect", patch=MethodPatch(inputs=[public])),
        ApplyStep(id="apply", method="revise"),
    )))
    frame = frame_for(world, "writer").reflection
    assert frame.materials[0].private_scope is None
    choice = capture_choice(world, scope, "propose", frame)
    peer_objects = objects_for(world, "position:reader")
    assert choice.id not in peer_objects
    assert not any(identifier.startswith("derived-") for identifier in peer_objects)
    assert not any(row.get("kind") == "view" for row in peer_objects.values())


def test_live_dependency_subscriptions_exclude_pinned_source(world):
    selected = public_ids(world)[0]
    initial = reflection_frame(world, "position:writer", [selected])
    version = next(row["version"] for row in initial.objects if row["id"] == selected)
    pinned = install(world, MethodDraft(title="固定来源", language_version=2,
                                       program=program(read(selected, mode="version", version=version))))
    frozen = frame_for(world, "writer").reflection
    assert selected not in frozen.live_dependencies
    assert any(ref.object_id == selected and ref.version == version for ref in frozen.dependencies)
    install(world, MethodDraft(title="订阅来源", language_version=2, revises=pinned.id,
                               program=program(read(selected))))
    assert selected in frame_for(world, "writer").reflection.live_dependencies


def test_reading_can_retrieve_entire_own_history_but_not_other_tracks(world):
    from opentale.domain import ReadingTrack, StorySegment

    world.reading_tracks[0].story = [StorySegment(
        id="old-reading", revision=0, text="旧日的潮汐带回那个尚未解释的承诺。",
        focus_position_id="writer", expression_ids=[], reinterprets=[],
    )]
    world.reading_tracks.append(ReadingTrack(id="second", title="另一条阅读"))
    scope = "reading:main"
    install(world, MethodDraft(title="回读", language_version=2, program=program(
        QueryStep(id="find", query="尚未解释的承诺", kinds=["content"], limit=1),
    )), scope=scope)
    frame = reflection_frame(world, scope, [])
    assert frame.selected_inputs == ["reading-segment:main:old-reading"]
    assert frame.materials[0].text == world.reading_tracks[0].story[0].text
    assert frame.selected_inputs[0] not in objects_for(world, "reading:second")
    assert frame.selected_inputs[0] not in objects_for(world, "position:writer")


def test_effect_can_be_queried_read_and_answered_as_a_reusable_object(world):
    scope = "position:writer"
    first, second = public_ids(world)[:2]
    capture_choice(world, scope, "propose", reflection_frame(world, scope, [first]))
    world.run.revision += 1
    after = capture_choice(world, scope, "propose", reflection_frame(world, scope, [second]))
    effect = world.reflection.effects[-1]
    row = objects_for(world, scope)[effect.id]
    assert row["changed"] and row["selected_removed"] == [first]
    assert row["about"] == [after.id, effect.previous_choice_id]
    assert "source_scope" not in row and "scope_id" not in row
    assert "新增1项" in row["text"] and "移出1项" in row["text"]
    method = install(world, MethodDraft(title="读取已发生的组织变化", language_version=2, program=program(
        QueryStep(id="find", query="组织变化", kinds=["effect"],
                  related_to=[ObjectRef(object_id=after.id)]),
    )))
    queried = frame_for(world, "writer").reflection
    assert queried.selected_inputs == [effect.id]
    assert queried.materials[0].role == "组织变化记录"
    install(world, MethodDraft(title="直接读取该后果", language_version=2, revises=method.id,
                               program=program(read(effect.id))))
    current = frame_for(world, "writer").reflection
    assert current.materials[0].text == row["text"]
    install_feedback(world, [(scope, ReflectionProposal(responses=[ResponseDraft(
        text="移出的材料使这次阅读的进入方式发生转向。", about=[effect.id],
        evidence=[after.id, effect.previous_choice_id], visibility="shared",
    )]), current)])
    response = world.reflection.responses[-1]
    assert response.id in objects_for(world, "position:reader")
    assert objects_for(world, scope)[effect.id]["version"] == row["version"]


def test_effect_taints_removed_private_material_and_private_inspection(world):
    scope = "position:writer"
    private = next(identifier for identifier, row in objects_for(world, scope).items()
                   if row.get("role") == "主体背景")
    public = public_ids(world)[0]
    first = capture_choice(world, scope, "propose", reflection_frame(world, scope, [private]))
    world.run.revision += 1
    after = capture_choice(world, scope, "propose", reflection_frame(world, scope, [public]))
    effect = world.reflection.effects[-1]
    assert effect.previous_choice_id == first.id and effect.selected_removed == [private]
    assert after.id in objects_for(world, "position:reader")
    assert effect.id in objects_for(world, scope)
    assert effect.id not in objects_for(world, "position:reader")
    install_feedback(world, [(scope, ReflectionProposal(responses=[ResponseDraft(
        text="这次移出涉及此前的组织。", about=[effect.id], visibility="shared",
    )]), reflection_frame(world, scope, []))])
    assert world.reflection.responses[-1].id not in objects_for(world, "position:reader")

    # Even public leaves can have private organizational causes.
    original = install(world, MethodDraft(title="仅在此处可见的定义", inputs=[private]))
    install(world, MethodDraft(title="检查之后仍读取公开材料", language_version=2,
                               revises=original.id, program=program(
                                   InspectStep(id="inspect", ref=ObjectRef(object_id=original.id)), read(public),
                               )))
    world.run.revision += 1
    choice = capture_choice(world, scope, "realize", frame_for(world, "writer").reflection)
    inspected_effect = world.reflection.effects[-1]
    assert choice.materials[0].private_scope is None
    assert inspected_effect.id not in objects_for(world, "position:reader")


def test_effect_versions_are_immutable_and_missing_previous_evidence_is_withheld(world):
    scope = "position:writer"
    selected = public_ids(world)[0]
    capture_choice(world, scope, "propose", reflection_frame(world, scope, [selected]))
    world.run.revision += 1
    capture_choice(world, scope, "propose", reflection_frame(world, scope, [selected]))
    effect = world.reflection.effects[-1]
    initial = objects_for(world, scope)[effect.id]
    version = initial["version"]
    assert not initial["changed"]
    install(world, MethodDraft(title="固定后果版本", language_version=2, program=program(
        read(effect.id, mode="version", version=version),
    )))
    world.run.revision += 1
    world.expressions.append(Expression(id="unrelated-later", text="另一个位置继续生活。", created_revision=2))
    capture_choice(world, scope, "realize", frame_for(world, "writer").reflection)
    assert objects_for(world, scope)[effect.id] == initial
    assert frame_for(world, "writer").reflection.materials[0].text == initial["text"]
    next(record for record in world.reflection.effects if record.id == effect.id).previous_choice_id = None
    assert effect.id not in objects_for(world, scope)


def test_empty_selected_input_still_exposes_a_scoped_choice_and_effect(world):
    frame = reflection_frame(world, "position:writer", [])
    choice = capture_choice(world, "position:writer", "propose", frame)
    effect = world.reflection.effects[-1]
    peer = objects_for(world, "position:reader")
    assert choice.id in peer and effect.id in peer
    assert peer[effect.id]["selected_added"] == []
    assert "本次采用0项材料" in peer[effect.id]["text"]


def test_legacy_method_ids_and_nested_frame_rows_match_pre_v2_serialization():
    """Golden 98110db method data: no new defaults may enter old IDs or dictionary rows."""
    from opentale.domain import Snapshot
    from opentale.reflection import legacy_reflection_data
    from opentale.reflection_types import ReflectionFrame

    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    initial = initialize(preset, "fixture", Limits(), "reflexive")
    scope = "position:writer"
    old_draft = {
        "title": "旧版组织", "instruction": "沿这段经历继续。",
        "inputs": ["use-keeper-e-letter-9fdc80cb0de7"], "order": "listed", "limit": 16, "revises": None,
    }
    old_method_id = "method-2565226926a38c51d7afc4ae"
    old_record = {**old_draft, "id": old_method_id, "created_revision": 0, "source_scope": scope}
    old_snapshot = initial.model_dump()
    old_snapshot["reflection"] = {"methods": [old_record], "adoptions": {scope: [old_method_id]}, "choices": []}
    restored = Snapshot.model_validate(old_snapshot)
    frame = reflection_frame(restored, scope, [])
    expected_method_row = {"id": old_method_id, "kind": "method", "text": old_draft["instruction"], **old_draft,
                           "preview_truncated": False}
    assert next(row for row in frame.objects if row["id"] == old_method_id) == expected_method_row
    assert all("private_scope" not in row for row in frame.objects)
    assert legacy_reflection_data(restored.reflection) == old_snapshot["reflection"]

    old_frame_keys = {"objects", "scopes", "active_methods", "selected_inputs", "groups", "materials",
                      "instruction", "input_fingerprint"}
    old_frame = {key: value for key, value in frame.model_dump().items() if key in old_frame_keys}
    old_frame["materials"] = [{key: value for key, value in material.items() if key != "private_scope"}
                              for material in old_frame["materials"]]
    restored_frame = ReflectionFrame.model_validate(old_frame)
    capture_choice(restored, scope, "propose", restored_frame)
    choice_row = next(row for row in reflection_frame(restored, scope, []).objects if row["kind"] == "choice")
    assert all("private_scope" not in material for material in choice_row["materials"])

    # Replay an old proposal against the old starting state, rather than generating fresh expectations.
    install_feedback(initial, [(scope, ReflectionProposal.model_validate({
        "methods": [old_draft], "adoptions": [{"scope_id": scope, "method_id": "draft:0", "action": "adopt"}],
    }), reflection_frame(initial, scope, []))])
    assert initial.reflection.methods[0].id == old_method_id
    assert legacy_reflection_data(initial.reflection)["methods"] == [old_record]
    assert reflection_frame(initial, scope, []).objects == old_frame["objects"]
    assert reflection_frame(initial, scope, []).input_fingerprint == old_frame["input_fingerprint"]
