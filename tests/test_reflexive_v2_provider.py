"""Versioned reflection programs preserve local capabilities and actual inputs."""

from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.policy_types import View, ViewGroup, ViewRelation
from opentale.providers import DeepSeekProvider, FixtureProvider, ProviderError
from opentale.providers.deepseek import _proposal_schema
from opentale.providers.policy import WireReflectionV2
from opentale.providers.reflection import compile_reflection, reflection_data
from opentale.reflection_types import Material
from pydantic import ValidationError
from test_reflexive_provider import actor_frame, envelope, reading_frame


def v2_frame(*, reading=False):
    frame = reading_frame() if reading else actor_frame()
    reflection = frame.reflection
    reflection.language_version = 2
    for index, row in enumerate(reflection.objects):
        row["version"] = f"version-{index}"
    reflection.objects.append({
        "id": "response-prior", "kind": "response", "text": "沿屋内的动作继续，雨衣成为归家的条件。",
        "about": ["method-old"], "evidence": ["object-rain"], "visibility": "shared",
        "version": "version-response", "source_scope": "private-creator",
    })
    reflection.full_objects = [{"id": "hidden-catalog", "kind": "content", "text": "full-catalog-sentinel"}]
    reflection.materials[0].private_scope = "private-creator"
    reflection.view = View(
        groups=[ViewGroup(id="root-technical", label="雨后", children=[
            ViewGroup(id="child-a", label="当前", material_ids=["object-rain"]),
            ViewGroup(id="child-b", label="回望", material_ids=["object-rain"]),
        ])], material_ids=["object-rain"],
        relations=[ViewRelation(source="child-a", target="child-b", label="归家的来处")],
    )
    return frame


def v2_wire():
    return {
        "methods": [{"title": "重看归家", "language_version": 2, "revises_index": 1,
                     "program": {"steps": [
                         {"id": "prior", "op": "inspect", "ref": {"object_index": 1, "mode": "version"}},
                         {"id": "changed", "op": "revise", "input": "prior", "patch": {
                             "program": {"steps": [
                                 {"id": "rain", "op": "read", "refs": [{"object_index": 0}]},
                                 {"id": "response", "op": "read", "refs": [{"object_index": 3}]},
                                 {"id": "relation", "op": "relate", "inputs": ["rain", "response"],
                                  "relations": [{"source": "rain", "target": "response", "label": "回应继续经验"}]},
                             ], "output": "relation"}}},
                         {"id": "used", "op": "apply", "method": "changed"},
                     ], "output": "used"}}],
        "adoptions": [{"scope_index": 0, "draft_index": 0}],
        "responses": [{"text": "归家的动作使雨后的等待有了去处。", "about_indices": [1],
                       "evidence_indices": [0], "visibility": "shared"}],
    }


def test_v2_compiles_nested_program_and_response_against_frozen_integer_capabilities():
    result = compile_reflection(WireReflectionV2.model_validate(v2_wire()), v2_frame().reflection)
    method = result.methods[0]
    assert method.language_version == 2 and method.revises == "method-old"
    inspect, revise, apply = method.program.steps
    assert inspect.ref.object_id == "method-old" and inspect.ref.version == "version-1"
    assert revise.patch.program.steps[1].refs[0].object_id == "response-prior"
    assert apply.method == "changed"
    assert result.responses[0].about == ["method-old"]
    assert result.responses[0].evidence == ["object-rain"]


def test_v2_schema_lists_current_handles_and_leaves_v1_schema_unchanged():
    schema = _proposal_schema(v2_frame())
    defs = schema["$defs"]
    assert defs["WireObjectRef"]["properties"]["object_index"]["enum"] == [0, 1, 2, 3]
    assert defs["WireResponse"]["properties"]["about_indices"]["items"]["enum"] == [0, 1, 2, 3]
    assert "WireReflectionV2" in defs
    classic = _proposal_schema(actor_frame())
    assert "WireReflectionV2" not in classic["$defs"]
    assert "action" not in classic["$defs"]["_FragmentTarget"]["properties"]


@pytest.mark.parametrize("mutation", ["unknown", "wrong_kind", "wrong_version", "forward_step", "response_unknown"])
def test_v2_bad_reference_and_programs_become_safe_retry_feedback(mutation):
    wire = v2_wire()
    program = wire["methods"][0]["program"]
    if mutation == "unknown":
        program["steps"][0]["ref"]["object_index"] = 999
    elif mutation == "wrong_kind":
        program["steps"][0]["ref"]["object_index"] = 0
    elif mutation == "wrong_version":
        program["steps"][0]["ref"]["version"] = "invented-version"
    elif mutation == "forward_step":
        program["steps"][2]["method"] = "future-step"
    else:
        wire["responses"][0]["about_indices"] = [999]
    with pytest.raises(ProviderError) as raised:
        compile_reflection(WireReflectionV2.model_validate(wire), v2_frame().reflection, usage=(5, 7))
    assert raised.value.code == "MODEL_OUTPUT_INVALID"
    assert raised.value.usage == (5, 7)
    assert raised.value.feedback.issues == ("REFLECTION_REFERENCE_INVALID",)
    assert "invented-version" not in str(raised.value)


def test_v2_raw_persistent_references_are_rejected_by_wire_schema():
    wire = v2_wire()
    wire["methods"][0]["program"]["steps"][0]["ref"] = {"object_id": "method-old"}
    with pytest.raises(ValidationError):
        WireReflectionV2.model_validate(wire)


def test_v2_patch_omission_and_explicit_unlimited_survive_compilation_roundtrip():
    wire = v2_wire()
    wire["methods"][0]["program"]["steps"][1]["patch"] = {"limit": None}
    compiled = compile_reflection(WireReflectionV2.model_validate(wire), v2_frame().reflection)
    patch = compiled.methods[0].program.steps[1].patch
    assert patch.model_dump(exclude_unset=True) == {"limit": None}
    restored = type(compiled).model_validate_json(compiled.model_dump_json())
    assert restored.methods[0].program.steps[1].patch.model_dump(exclude_unset=True) == {"limit": None}
    wire["methods"][0]["program"]["steps"][1]["patch"] = {"order": None}
    with pytest.raises(ValidationError):
        WireReflectionV2.model_validate(wire)


def test_full_materials_and_nested_view_keep_semantics_without_catalog_or_private_metadata():
    frame = v2_frame()
    text = "完整的源段落。" * 3000
    frame.reflection.materials[0].text = text
    projection = reflection_data(frame.reflection)
    assert projection["materials"][0]["text"] == text
    assert projection["view"]["groups"][0]["children"][0]["material_indices"] == [0]
    assert projection["view"]["relations"] == [{"source_group_index": 1, "target_group_index": 2,
                                               "label": "归家的来处"}]
    assert projection["objects"][3]["about_indices"] == [1]
    raw = json.dumps(projection, ensure_ascii=False)
    for hidden in ("private-creator", "private_scope", "full-catalog-sentinel", "root-technical", "child-a"):
        assert hidden not in raw


def test_executed_definitions_are_complete_addressed_and_keep_provenance_private():
    frame = v2_frame()
    instruction = "把雨后的生活读成归家的动作。" * 100
    frame.reflection.definitions = [{
        "object_id": "method-old", "operation": "inspect", "title": "旧组织",
        "instruction": instruction, "inputs": ["object-rain", "unavailable-private-id"],
        "language_version": 1, "order": "listed", "limit": None,
        "source_scope": "private-creator",
    }]
    projection = reflection_data(frame.reflection)
    definition = projection["executed_definitions"][0]
    assert definition["instruction"] == instruction
    assert definition["operation"] == "inspect"
    assert definition["object_index"] == 1 and definition["object_indices"] == [0]
    assert definition["unavailable_input_count"] == 1
    assert "unavailable-private-id" not in json.dumps(projection)
    assert "private-creator" not in json.dumps(projection)
    realized = json.loads(DeepSeekProvider(Settings()).request_projection(frame, "realize")["messages"][1]["content"])
    assert "executed_definitions" not in realized


def test_view_projection_preserves_repeated_material_occurrences_in_sequence():
    frame = v2_frame()
    frame.reflection.materials = [Material(id=identity, text=identity) for identity in ["A", "B", "A", "B"]]
    frame.reflection.view = View(material_ids=["A", "B", "A", "B"], groups=[
        ViewGroup(id="first", material_ids=["A", "B"]),
        ViewGroup(id="second", material_ids=["A", "B"]),
    ], relations=[ViewRelation(source="first", target="second", label="回望")])
    view = reflection_data(frame.reflection)["view"]
    assert view["material_indices"] == [0, 1, 2, 3]
    assert [group["material_indices"] for group in view["groups"]] == [[0, 1], [2, 3]]
    assert view["relations"][0]["source_group_index"] != view["relations"][0]["target_group_index"]


def test_effect_projection_uses_local_handles_and_marks_absent_historical_leaves():
    frame = v2_frame()
    frame.reflection.objects.extend([
        {"id": "choice-new", "kind": "choice", "text": "这次选择", "selected": ["object-rain"]},
        {"id": "effect-change", "kind": "effect", "text": "当前组织发生变化，新增一项材料。",
         "changed": True, "stage": "propose", "choice_id": "choice-new", "previous_choice_id": "choice-old",
         "method_ids": ["method-old"], "selected_added": ["object-rain"],
         "selected_removed": ["removed-outside-table"], "response_ids": ["response-prior"],
         "about": ["choice-new", "choice-old"], "evidence": ["choice-new", "choice-old"],
         "source_scope": "private-creator", "before_fingerprint": "hidden-before-hash", "private_scope": "position:a"},
    ])
    row = reflection_data(frame.reflection)["objects"][-1]
    assert row["changed"] is True and row["stage"] == "propose"
    assert row["choice_index"] == 4 and row["previous_choice_index"] == 2
    assert row["method_indices"] == [1] and row["response_indices"] == [3]
    assert row["selected_added_indices"] == [0]
    assert row["selected_removed_indices"] == [] and row["unavailable_selected_removed_count"] == 1
    assert row["about_indices"] == [4, 2] and row["evidence_indices"] == [4, 2]
    raw = json.dumps(row)
    for hidden in ("effect-change", "choice-new", "choice-old", "method-old", "removed-outside-table",
                   "source_scope", "private-creator", "hidden-before-hash", "private_scope"):
        assert hidden not in raw


def test_effect_can_be_queried_read_and_referenced_by_local_response():
    frame = v2_frame()
    frame.reflection.objects.append({"id": "effect-observed", "kind": "effect", "text": "组织发生变化。"})
    wire = WireReflectionV2.model_validate({
        "methods": [{"title": "取得组织后果", "language_version": 2, "program": {
            "steps": [
                {"id": "effects", "op": "query", "query": "组织", "kinds": ["effect"]},
                {"id": "observed", "op": "read", "refs": [{"object_index": 4}]},
                {"id": "joined", "op": "group", "inputs": ["effects", "observed"], "label": "组织的经历"},
            ], "output": "joined"}}],
        "responses": [{"text": "这次变化使先前的经历共同进入。", "about_indices": [4], "evidence_indices": [2]}],
    })
    compiled = compile_reflection(wire, frame.reflection)
    assert compiled.methods[0].program.steps[0].kinds == ["effect"]
    assert compiled.methods[0].program.steps[1].refs[0].object_id == "effect-observed"
    assert compiled.responses[0].about == ["effect-observed"]
    assert "effect" in _proposal_schema(frame)["$defs"]["WireQuery"]["properties"]["kinds"]["items"]["enum"]


@pytest.mark.parametrize("stage", ["propose", "realize", "present"])
async def test_v2_stages_use_exact_safe_projection_and_compile_programs(stage):
    frame = v2_frame(reading=stage == "present")
    response = {"text": "她将雨衣挂上木钩，走进厨房。"}
    if stage == "propose":
        response = {"fragments": [{"text": response["text"], "targets": []}], "reflection": v2_wire()}
    elif stage == "present":
        response.update(focus_index=0, expression_indices=[], reinterprets_indices=[], reflection=v2_wire())
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(response))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key="mock-only-secret"), client)
        expected = provider.request_projection(frame, stage)
        result = await getattr(provider, stage)(frame)
    assert expected == captured[0]
    data = json.loads(captured[0]["messages"][1]["content"])
    actual = data if stage == "realize" else data["reflection"]
    assert actual["view"]["relations"][0]["label"] == "归家的来处"
    assert "private_scope" not in json.dumps(data)
    assert "full-catalog-sentinel" not in json.dumps(data)
    if stage != "realize":
        assert result.value.reflection.methods[0].language_version == 2
        assert result.value.reflection.responses[0].about == ["method-old"]
    else:
        assert "objects" not in actual


async def test_v2_withdraw_is_explicit_target_action():
    frame = v2_frame()
    frame.peer_interfaces = [{"position_id": "person-b", "expression_id": "letter", "role": "回信"}]
    response = {"fragments": [{"text": "她收起旧信。", "targets": [
        {"interface_index": 0, "role": "回信", "action": "withdraw"}]}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope(response)),
    )) as client:
        result = await DeepSeekProvider(Settings(api_key="mock-only-secret"), client).propose(frame)
    assert result.value.operations[0].op == "withdraw"
    assert result.value.operations[0].target_position_id == "person-b"


async def test_v2_fixture_revises_actual_method_with_prior_response_as_new_material():
    frame = v2_frame()
    # Material privacy metadata is intentionally separate from the public row.
    proposal = (await FixtureProvider().propose(frame)).value.reflection
    method = proposal.methods[0]
    assert [step.op for step in method.program.steps] == ["inspect", "revise", "apply"]
    next_program = method.program.steps[1].patch.program
    assert next_program.steps[1].refs[0].object_id == "response-prior"
    assert proposal.responses[0].about == ["method-old"]
    assert proposal.responses[0].visibility == "shared"
    projection = FixtureProvider().request_projection(frame, "propose")
    assert "full-catalog-sentinel" not in json.dumps(projection)
    assert "private_scope" not in json.dumps(projection)


async def test_v2_fixture_uses_relationship_structure_even_when_materials_stay_equal():
    frame = v2_frame()
    first = (await FixtureProvider().realize(frame)).value
    frame.reflection.view.relations[0].label = "离别带来的回望"
    second = (await FixtureProvider().realize(frame)).value
    assert first != second
    assert "离别带来的回望" in second


async def test_v2_fixture_response_uses_observed_effect_as_evidence_when_available():
    frame = v2_frame()
    frame.reflection.objects.append({
        "id": "effect-observed", "kind": "effect", "text": "新增一项材料，关系发生变化。",
        "method_ids": ["method-old"], "changed": True,
    })
    response = (await FixtureProvider().propose(frame)).value.reflection.responses[0]
    assert "effect-observed" in response.about and "effect-observed" in response.evidence
    assert "新增一项材料，关系发生变化。" in response.text
