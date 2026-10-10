from __future__ import annotations

import copy
import json

import httpx
import pytest
from opentale.config import Settings
from opentale.providers import ProviderError
from opentale.providers.growth import (
    DeepSeekGrowthProvider,
    FixtureGrowthProvider,
    make_growth_provider,
    project_frame,
    validate_decision,
)

KEY = "offline-growth-test-key"


def frame(role="window"):
    method = {"id": "method-old", "version": "version-old", "instruction": "接住门前的动作，让人物向共同入口继续。"}
    return {
        "scope_id": "scope-a", "session_id": "session-a", "role": role,
        "graph_commit": 3, "work_seq": 0, "direction": "接住门前的生活",
        "cursor": "node-a", "frontier": ["node-a"],
        "allowed_actions": ["query", "read_page", "inspect", "apply", "revise", "adopt", "try_write",
                            "publish", "navigate", "render", "respond", "wait"],
        "nodes": [{"id": "node-a", "version": "version-a", "text_preview": "她停在门前。"},
                  {"id": "node-b", "version": "version-b", "text_preview": "他把杯子放下。"}],
        "methods": [copy.deepcopy(method)], "method": method,
        "links": [{"id": "edge-a-b", "source_id": "node-a", "target_id": "node-b", "kind": "growth", "relation": "继续"}],
        "sources": [{"id": "node-a", "version": "version-a", "text": "她停在门前，把手放在门闩上。", "start": 0, "end": 15}],
        "trials": [], "visits": [], "responses": [], "receipts": [], "pages": [],
    }


def envelope(value, *, finish="stop", content=None):
    return {
        "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False) if content is None else content,
                                   "reasoning_content": "private transport reasoning"}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 81, "completion_tokens": 23},
    }


def record(frame, action, *, details=None):
    frame["receipts"].append({"id": f"receipt-{len(frame['receipts'])}", "action": action, "details": details or {}})
    frame["work_seq"] += 1


async def test_fixture_real_method_feedback_revision_adoption_reapplication_chain():
    provider = FixtureGrowthProvider()
    current = frame()
    outputs = []
    actions = []
    for _ in range(9):
        result = await provider.decide(current)
        action, args = result.value["action"], result.value["args"]
        actions.append(action)
        if action == "try_write":
            text = (await provider.draft(current)).value
            outputs.append(text)
            trial = {"id": f"trial-{len(outputs)}", "text": text, "method_id": current["method"]["id"]}
            current["pending_trial"] = trial
            current["trials"].append(copy.deepcopy(trial))
        elif action == "respond":
            assert args["about_ids"] == [current["pending_trial"]["id"]]
            current["responses"].append({"id": "response-trial", "text": args["text"]})
        elif action == "revise":
            assert args["input_ids"] == ["response-trial"]
            current["methods"].append({
                "id": "method-new", "version": "version-new", "instruction": args["instruction"],
                "parent_ref": {"kind": "method", "object_id": args["method_id"], "version": "version-old"},
            })
        elif action == "adopt":
            assert args["method_id"] == "method-new"
            current["method"] = current["methods"][-1]
        elif action == "apply":
            assert args["method_id"] == current["method"]["id"]
        elif action == "publish":
            assert args["trial_id"] == "trial-2"
        record(current, action, details=args)
    assert actions == ["inspect", "apply", "try_write", "respond", "revise", "adopt", "apply", "try_write", "publish"]
    assert outputs[0] != outputs[1]
    assert "各自说出要去的地方" in outputs[1]
    assert current["responses"][0]["text"] in json.dumps(provider.projection(current, "decide"), ensure_ascii=False)


async def test_fixture_reader_navigates_and_renders_same_graph_independently():
    provider = FixtureGrowthProvider()
    reader = frame("reading")
    reader["visits"] = [{"id": "visit-a", "node_id": "node-a"}]
    growth = frame()
    actions = []
    for _ in range(4):
        decision = (await provider.decide(reader)).value
        actions.append(decision["action"])
        if decision["action"] == "navigate":
            assert decision["args"] == {"node_id": "node-b", "edge_id": "edge-a-b", "transition": "edge"}
            reader["visits"].append({"id": "visit-b", "node_id": "node-b"})
            reader["cursor"] = "node-b"
            reader["sources"] = [{"id": "node-b", "version": "version-b", "text": "他把杯子放下。"}]
        if decision["action"] == "render":
            assert decision["args"] == {"visit_ids": ["visit-b"]}
            assert (await provider.render(reader)).value == "他把杯子放下。"
        record(reader, decision["action"], details=decision["args"])
    assert actions == ["apply", "navigate", "render", "respond"]
    assert reader["graph_commit"] == growth["graph_commit"] == 3
    assert growth["work_seq"] == 0
    assert (await provider.decide(growth)).value["action"] == "inspect"


@pytest.mark.parametrize("decision", [
    {"action": "adopt", "args": {"method_id": "method-unoffered"}},
    {"action": "publish", "args": {"trial_id": "trial-unoffered"}},
    {"action": "read_page", "args": {"kind": "response", "id": "node-a"}},
    {"action": "read_page", "args": {"kind": "node", "id": "node-a", "start": 5, "end": 2}},
    {"action": "read_page", "args": {"kind": "node", "id": "node-a", "start": True}},
    {"action": "try_write", "args": {"parent_ids": ["private-node"]}},
    {"action": "try_write", "args": {"parent_ids": ["node-a", "node-a"]}},
    {"action": "revise", "args": {"method_id": "method-old", "instruction": "", "input_ids": []}},
    {"action": "navigate", "args": {"node_id": "node-a", "edge_id": "edge-unoffered"}},
    {"action": "navigate", "args": {"node_id": "node-b", "transition": "revisit"}},
    {"action": "navigate", "args": {"node_id": "node-b", "transition": "edge"}},
    {"action": "render", "args": {"visit_ids": []}},
    {"action": "respond", "args": {"text": "feedback", "about_ids": ["unoffered"]}},
    {"action": "wait", "args": {"secret_field": "private error text"}},
    {"action": "wait", "args": {}, "reasoning": "private reasoning"},
])
def test_action_shapes_and_handles_reject_without_exposing_values(decision):
    with pytest.raises(ProviderError) as captured:
        validate_decision(frame(), decision)
    assert captured.value.code == "MODEL_OUTPUT_INVALID"
    assert captured.value.retryable
    assert captured.value.feedback is not None
    assert "private" not in str(captured.value)
    assert "unoffered" not in str(captured.value)


def test_source_capability_is_not_granted_by_name_and_offered_actions_apply():
    current = frame()
    current["allowed_actions"] = ["read_page", "wait"]
    assert validate_decision(current, {"action": "read_page", "args": {"kind": "node", "id": "node-a", "start": 15, "end": 30}})
    with pytest.raises(ProviderError):
        validate_decision(current, {"action": "apply", "args": {"method_id": "method-old"}})


def test_real_query_page_reoffers_handles_beyond_initial_catalog():
    current = frame()
    current["pages"] = [{"kind": "response", "offset": 100, "next_offset": 200,
                         "items": [{"id": "response-next", "version": "response-version", "text_preview": "回应后页"}]}]
    decision = {"action": "read_page", "args": {"kind": "response", "id": "response-next", "start": 0, "end": 10}}
    assert validate_decision(current, decision) == decision
    projection = DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    assert "response-next" in projection["messages"][0]["content"]


def test_feedback_directory_reoffers_full_response_page_without_blanking_delivered_feedback():
    current = frame()
    current["responses"] = [{"id": "response-first", "text": "这一处反馈完整保留。"}]
    current["feedback_catalog"] = [{"id": "response-first", "version": "v1", "length": 13},
                                   {"id": "response-other", "version": "v2", "length": 7000}]
    decision = {"action": "read_page", "args": {"kind": "response", "id": "response-other", "start": 0, "end": 2500}}
    assert validate_decision(current, decision) == decision
    projected = project_frame(current, "decide")
    assert projected["responses"][0]["text"] == "这一处反馈完整保留。"
    assert projected["feedback_catalog"][1]["length"] == 7000


def test_delivered_reading_history_is_readable_evidence_without_becoming_world_node():
    current = frame("reading")
    current["reading_history"] = [{
        "id": "passage-a", "version": "passage-version-a", "text": "先前的阅读只跟着门前的脚步。",
        "start": 0, "end": 16, "full_length": 16,
    }]
    for decision in [
        {"action": "respond", "args": {"text": "这次把杯子的回应接入路径。", "about_ids": ["passage-a"]}},
        {"action": "revise", "args": {
            "method_id": "method-old", "instruction": "从先前阅读的选择中找出被略过的回应，进入相连的另一枝。",
            "input_ids": ["passage-a"],
        }},
    ]:
        assert validate_decision(current, decision) == decision
    for decision in [
        {"action": "navigate", "args": {"node_id": "passage-a", "transition": "entry"}},
        {"action": "revise", "args": {"method_id": "method-old", "instruction": "选另一枝。",
                                         "program": {"node_ids": ["passage-a"]}}},
    ]:
        with pytest.raises(ProviderError):
            validate_decision(current, decision)
    for decision in [
        {"action": "read_page", "args": {"kind": "passage", "id": "passage-a"}},
        {"action": "query", "args": {"kind": "passage", "query": "*"}},
    ]:
        assert validate_decision(current, decision) == decision
    projection = DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    body = json.loads(projection["messages"][1]["content"])
    assert body["reading_history"] == current["reading_history"]
    assert '"passage-a"' in projection["messages"][0]["content"]


def test_late_link_query_page_offers_endpoints_and_navigation_handles():
    current = frame("reading")
    current["links"] = []
    current["nodes"] = []
    current["cursor"] = "node-early-outside-catalog"
    query = {"action": "query", "args": {"kind": "link", "query": "*", "offset": 100}}
    assert validate_decision(current, query) == query
    current["pages"] = [{"kind": "link", "offset": 100, "items": [{
        "id": "edge-late", "version": "edge-version", "source_id": current["cursor"],
        "target_id": "node-late", "kind": "growth", "relation": "继续",
    }]}]
    navigation = {"action": "navigate", "args": {"node_id": "node-late", "edge_id": "edge-late", "transition": "edge"}}
    assert validate_decision(current, navigation) == navigation
    assert validate_decision(current, {"action": "read_page", "args": {"kind": "link", "id": "edge-late"}})
    system = DeepSeekGrowthProvider(Settings()).projection(current, "decide")["messages"][0]["content"]
    assert "edge-late" in system and "node-late" in system


def test_link_definition_and_view_pages_offer_structural_handles():
    current = frame("reading")
    current["links"] = []
    current["views"] = [{"id": "view-catalog", "version": "view-version", "members": []}]
    current["pages"] = [{"kind": "link", "id": "edge-page", "object": {
        "id": "edge-page", "version": "edge-page-version", "source_id": "node-a", "target_id": "node-b",
    }}, {"kind": "view", "id": "view-page", "object": {
        "id": "view-page", "version": "view-page-version", "members": [
            {"kind": "node", "object_id": "node-member", "version": "member-version"}],
    }}]
    for kind, key in (("link", "edge-page"), ("view", "view-catalog"), ("view", "view-page")):
        assert validate_decision(current, {"action": "read_page", "args": {"kind": kind, "id": key}})
    assert validate_decision(current, {"action": "query", "args": {"kind": "view", "query": "*"}})
    assert validate_decision(current, {"action": "navigate", "args": {"node_id": "node-member", "transition": "entry"}})
    assert project_frame(current, "decide")["views"] == current["views"]


@pytest.mark.parametrize("ranges", [{"start": -1}, {"start": True}, {"end": False}, {"start": 5, "end": 5},
                                    {"start": 5, "end": 2}, {"start": 10}, {"end": 11}])
def test_navigation_range_is_strict_and_stays_within_known_codepoint_length(ranges):
    current = frame("reading")
    current["nodes"][0]["text_ref"] = {"sha256": "body-hash", "length": 10}
    with pytest.raises(ProviderError) as captured:
        validate_decision(current, {"action": "navigate", "args": {"node_id": "node-a", "transition": "entry", **ranges}})
    assert captured.value.code == "MODEL_OUTPUT_INVALID"


def test_partial_navigation_range_is_legal_and_in_schema():
    current = frame("reading")
    current["nodes"][0]["text_ref"] = {"sha256": "body-hash", "length": 10}
    decision = {"action": "navigate", "args": {"node_id": "node-a", "transition": "entry", "start": 1, "end": 4}}
    assert validate_decision(current, decision) == decision
    system = DeepSeekGrowthProvider(Settings()).projection(current, "decide")["messages"][0]["content"]
    assert '"start"' in system and '"end"' in system


def test_revision_program_and_feedback_criteria_only_use_offered_handles():
    current = frame()
    program = {"node_ids": ["node-b", "node-a"], "groups": {"门前": ["node-a", "node-b"]},
               "relations": [{"source_id": "node-a", "target_id": "node-b", "relation": "彼此回应", "kind": "association"}]}
    decision = {"action": "revise", "args": {"method_id": "method-old", "instruction": "按两处回应组织生活。", "program": program}}
    assert validate_decision(current, decision) == decision
    feedback = {"action": "respond", "args": {"text": "两个方向留下了不同后果。", "evaluation_method_id": "method-old"}}
    assert validate_decision(current, feedback) == feedback
    system = DeepSeekGrowthProvider(Settings()).projection(current, "decide")["messages"][0]["content"]
    assert "evaluation_method_id" in system and '"program"' in system and '"groups"' in system


def test_unchanged_revision_is_valid_work_with_host_owned_nochange_detection():
    current = frame()
    current["method"]["program"] = {"node_ids": ["node-a"]}
    current["responses"] = [{"id": "response-a", "text": "同一结果仍然出现。"}]
    decision = {"action": "revise", "args": {
        "method_id": "method-old", "instruction": current["method"]["instruction"],
        "input_ids": ["response-a"], "program": {"node_ids": ["node-a"]},
    }}
    assert validate_decision(current, decision, usage=(81, 23)) == decision
    current["receipts"] = [{"action": "revise", "details": {"changed": False}}]
    system = DeepSeekGrowthProvider(Settings()).projection(current, "decide")["messages"][0]["content"]
    assert "宿主复用了原版本" in system
    assert "改变选材、组合调用或检验方式" in system


def test_node_evidence_preserves_unchanged_explicit_selection_for_host_review():
    current = frame()
    current["method"]["program"] = {"node_ids": ["node-a"]}
    decision = {"action": "revise", "args": {
        "method_id": "method-old", "instruction": current["method"]["instruction"],
        "input_ids": ["node-b"], "program": {"node_ids": ["node-a"]},
    }}
    assert validate_decision(current, decision) == decision


@pytest.mark.parametrize("change", [
    {"program": {"node_ids": ["node-b", "node-a"]}},
    {"program": {"groups": {"两种回应": ["node-a", "node-b"]}}},
    {"program": {"relations": [{"source_id": "node-a", "target_id": "node-b", "relation": "回应"}]}},
    {"input_ids": ["node-b"]},
    {"instruction": "先把两处人物的实际回应并置，再从差异选择后续行动。"},
])
def test_method_revision_can_change_real_organization_with_same_instruction(change):
    current = frame()
    current["method"]["program"] = {"node_ids": ["node-a"]}
    decision = {"action": "revise", "args": {
        "method_id": "method-old", "instruction": current["method"]["instruction"], **change,
    }}
    assert validate_decision(current, decision) == decision


def test_shortened_method_catalog_is_never_an_equality_witness_for_noop_revision():
    current = frame()
    current["methods"].append({"id": "method-other", "version": "other-version", "instruction": "简介"})
    decision = {"action": "revise", "args": {"method_id": "method-other", "instruction": "简介"}}
    assert validate_decision(current, decision) == decision
    current["pages"] = [{"method": {"id": "method-other", "version": "other-version", "instruction": "简介"}}]
    assert validate_decision(current, decision) == decision


def test_method_iteration_prompt_reaches_model_with_real_evidence_and_reader_role():
    current = frame("reading")
    current["receipts"] = [{"action": "render", "details": {"visit_ids": ["visit-a"]}}]
    current["responses"] = [{"id": "response-a", "text": "两次阅读都重述门闩，却未展开两个人对去处的差异。"}]
    current["method_work"] = {"bootstrap": True, "candidate_refs": []}
    current["reading_work"] = {"unrendered_visit_ids": ["visit-a"]}
    projection = DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    system = projection["messages"][0]["content"]
    body = json.loads(projection["messages"][1]["content"])
    assert "revise 只保存候选" in system
    assert "program.node_ids" in system and "cursor/frontier" in system
    assert "条件下使用" in system and "从结果判断效果" in system
    assert "方法可以继续成为其他方法的输入" in system
    assert "navigate/render" in system and "常规呈现" in system
    assert "刚产生了实际结果" in system
    assert body["responses"] == current["responses"]
    assert body["method_work"] == current["method_work"]
    assert body["reading_work"] == current["reading_work"]


def test_work_observations_expose_real_open_work_without_forcing_reflection_order():
    current = frame("reading")
    current["method_work"] = {
        "bootstrap": True,
        "adopted_ref": {"kind": "method", "object_id": "method-old", "version": "version-old"},
        "applied_ref": None,
        "candidate_refs": [{"kind": "method", "object_id": "method-other", "version": "version-other"}],
        "recent_result_refs": [], "recent_feedback_refs": [], "recent_actions": [],
        "pending_trial_unpublished": True,
    }
    current["reading_work"] = {"unrendered_visit_ids": ["visit-a"], "recent_passage_ids": []}
    provider = DeepSeekGrowthProvider(Settings())
    projected = provider.projection(current, "decide")
    system = projected["messages"][0]["content"]
    assert "优先 render reading_work.unrendered_visit_ids" in system
    assert "当前仍采用初始通用方法" in system
    assert "已有当前未采用的方法候选" in system
    assert "当前正在采用或临时试用的方法尚未完成本版本组织" in system
    assert "尚未发布的试写" in system
    assert "direction 简述本次局部探索的问题" in system
    assert "instruction 描述可多次使用的操作与检验" in system
    assert "持续工作方式的变化通过 revise/adopt" in system
    # Guidance observes work, while the finite menu still permits keeping an
    # effective method, waiting, inspecting, and revising at arbitrary depth.
    for action, args in [
        ("wait", {}), ("inspect", {"method_id": "method-old"}), ("apply", {"method_id": "method-old"}),
        ("revise", {"method_id": "method-old", "instruction": "先接住门外的回应，检验去处差异。"}),
    ]:
        assert validate_decision(current, {"action": action, "args": args})


@pytest.mark.parametrize("organized", [False, True])
def test_temporary_candidate_trial_guidance_follows_active_exact_method_not_formal_adoption(organized):
    current = frame()
    old = {"kind": "method", "object_id": "method-old", "version": "version-old"}
    active = {"kind": "method", "object_id": "method-candidate", "version": "version-candidate"}
    current["method"] = {"id": active["object_id"], "version": active["version"],
                         "instruction": "把两种回应并置，检验各自的后果。"}
    current["method_work"] = {"adopted_ref": old, "applied_ref": active if organized else old}
    system = DeepSeekGrowthProvider(Settings()).projection(current, "decide")["messages"][0]["content"]
    assert ("当前正在采用或临时试用的方法尚未完成本版本组织" in system) is not organized
    assert "apply 该候选执行组织并" in system
    assert "临时试用" in system and "adopt 正式采用" in system
    assert "adopt 后才能影响" not in system


@pytest.mark.parametrize("stage", ["decide", "draft", "render"])
def test_positive_experiential_style_guides_methods_feedback_and_all_prose_stages(stage):
    projection = DeepSeekGrowthProvider(Settings()).projection(frame(), stage)
    system = projection["messages"][0]["content"]
    assert "以正在发生的事组织句子" in system
    assert "表达以肯定、具体、可感知的陈述为主" in system
    assert "有情节依据的拒绝、否认、缺失与冲突" in system
    assert "direction、instruction、responses" in system
    assert "方法与评议也采用正向可执行的表述" in system


@pytest.mark.parametrize("stage", ["draft", "render"])
async def test_grounded_refusal_and_denial_remain_valid_narrative(stage):
    text = '她握住门闩：“我不跟你走。”他放下杯子，回答自己没有见过那封信。'
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"text": text}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await getattr(DeepSeekGrowthProvider(Settings(api_key=KEY), client), stage)(frame())
    assert result.value == text
    assert len(requests) == 1


@pytest.mark.parametrize("program", [{"node_ids": ["node-a"], "purpose": "回应仍然有效"},
                                    {"node_ids": ["node-a"], "groups": {"同一入口": ["node-a"]}}])
def test_omitting_purpose_preserves_existing_method_goal_for_host_comparison(program):
    current = frame()
    current["method"]["program"] = program
    decision = {"action": "revise", "args": {
        "method_id": "method-old", "instruction": current["method"]["instruction"],
    }}
    assert validate_decision(current, decision) == decision


async def test_safe_typed_handle_feedback_can_repair_an_unoffered_reference():
    current = frame()
    current["method"]["program"] = {"node_ids": ["node-a"]}
    current["responses"] = [{"id": "response-a", "version": "response-v", "text": "把杯子的回应接入下一段。"}]
    received = []
    def handler(request):
        received.append(json.loads(request.content))
        args = {"method_id": "method-old", "instruction": "先接住门外的回应，再选择去处。",
                "input_ids": ["unoffered-private-reference"]}
        if len(received) > 1:
            args["input_ids"] = ["response-a"]
        return httpx.Response(200, json=envelope({"action": "revise", "args": args}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY), client)
        with pytest.raises(ProviderError) as captured:
            await provider.decide(current)
        repaired = await provider.decide(current, repair=captured.value.feedback)
    assert repaired.value["args"]["input_ids"] == ["response-a"]
    system = received[1]["messages"][0]["content"]
    assert "input_ids:schema_invalid" in system and "HANDLE_UNKNOWN" in system
    assert "unoffered-private-reference" not in system
    assert len(received) == 2


@pytest.mark.parametrize("program", [
    {"node_ids": ["private-node"]}, {"groups": {"位置": ["private-node"]}},
    {"groups": []}, {"groups": {"位置": "node-a"}},
    {"relations": [{"source_id": "node-a", "target_id": "private-node"}]},
    {"relations": [{"source_id": "node-a", "target_id": "node-b", "kind": []}]},
    {"relations": [{"source_id": "node-a", "target_id": "node-b", "code": "executable code"}]},
    {"code": "executable code"},
])
def test_revision_program_rejects_unknown_or_untyped_structure(program):
    with pytest.raises(ProviderError) as captured:
        validate_decision(frame(), {"action": "revise", "args": {"method_id": "method-old", "instruction": "继续。", "program": program}})
    assert captured.value.code == "MODEL_OUTPUT_INVALID"


async def test_fixture_writing_uses_bounded_source_cue_without_copying_cumulative_body():
    current = frame()
    current["sources"][0]["text"] = "她拿起门闩。" + "旧的生活继续积累。" * 3000
    text = (await FixtureGrowthProvider().draft(current)).value
    assert text.startswith("她拿起门闩。")
    assert len(text) < 160
    assert "旧的生活继续积累。" not in text


def test_method_structure_and_exact_evidence_are_preserved_in_projection():
    current = frame()
    current["view"] = {"id": "view-shared", "members": [{"object_id": "node-a", "version": "version-a"}], "relations": []}
    current["method_evidence"] = [{"id": "response-exact", "version": "evidence-v", "text": "实际反馈全文"}]
    current["method_dependencies"] = [{"kind": "response", "object_id": "response-exact", "version": "evidence-v"}]
    projected = project_frame(current, "draft")
    for field in ("view", "method_evidence", "method_dependencies"):
        assert projected[field] == current[field]


async def test_fixture_adopted_revision_survives_recent_receipt_window():
    current = frame()
    current["method"] = {"id": "method-new", "version": "version-new", "instruction": "接住回应继续生活。",
                         "parent_ref": {"kind": "method", "object_id": "method-old", "version": "version-old"}}
    current["methods"].append(copy.deepcopy(current["method"]))
    current["receipts"] = [{"action": "apply"}, {"action": "try_write"}]
    current["pending_trial"] = {"id": "trial-new", "text": "她走到台阶下。"}
    assert (await FixtureGrowthProvider().decide(current)).value == {"action": "publish", "args": {"trial_id": "trial-new"}}


def test_projection_keeps_exact_method_feedback_receipts_and_read_page_ranges():
    current = frame()
    feedback = "完整回应最后仍有一处具体选择。" * 300
    current["responses"] = [{"id": "response-a", "version": "response-v", "text": feedback}]
    current["receipts"] = [{"id": "receipt-a", "action": "read_page", "details": {"source": "node-a", "start": 0, "end": 15}}]
    current["pages"] = [{"kind": "node", "id": "node-a", "version": "version-a", "text": current["sources"][0]["text"],
                         "start": 0, "end": 15, "next_start": 15, "complete": False}]
    current["settings"] = {"api_key": KEY, "base_url": "hidden endpoint"}
    current["responses"][0]["reasoning_content"] = "hidden feedback reasoning"
    before = copy.deepcopy(current)
    provider = DeepSeekGrowthProvider(Settings(api_key=KEY))
    projection = provider.projection(current, "decide")
    body = json.loads(projection["messages"][1]["content"])
    assert body["responses"][0]["text"] == feedback
    assert body["method"] == current["method"]
    assert body["pages"][0]["next_start"] == 15
    assert body["receipts"] == current["receipts"]
    serialized = json.dumps(projection, ensure_ascii=False)
    assert KEY not in serialized
    assert "hidden feedback reasoning" not in serialized
    assert "hidden endpoint" not in serialized
    assert current == before
    assert projection["thinking"] == {"type": "disabled"}
    assert "active_uses" not in serialized
    assert "不是X而是Y" not in serialized


async def test_actual_read_page_decision_is_transmitted_and_freezes_exact_request():
    current = frame()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"action": "read_page", "args": {"kind": "node", "id": "node-a", "start": 15, "end": 30}}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY), client)
        frozen = provider.projection(current, "decide")
        result = await provider.decide(current)
    assert result.value == {"action": "read_page", "args": {"kind": "node", "id": "node-a", "start": 15, "end": 30}}
    assert (result.input_tokens, result.output_tokens) == (81, 23)
    assert seen == [frozen]


async def test_malformed_action_feedback_repairs_using_safe_codes_and_accounts_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, json=envelope({"action": "adopt", "args": {"method_id": "secret-unoffered-model-value"}}))
        return httpx.Response(200, json=envelope({"action": "inspect", "args": {"method_id": "method-old"}}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY), client)
        with pytest.raises(ProviderError) as captured:
            await provider.decide(frame())
        assert captured.value.usage == (81, 23)
        result = await provider.decide(frame(), repair=captured.value.feedback)
    assert result.value["action"] == "inspect"
    assert "SCHEMA_VALIDATION" in seen[1]["messages"][0]["content"]
    assert "secret-unoffered-model-value" not in json.dumps(seen)


@pytest.mark.parametrize("stage", ["draft", "render"])
async def test_prose_stages_use_same_transport_separate_versioned_method_and_actual_text(stage):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"text": "她把门闩抬起来，看见他站在台阶下。"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY), client)
        result = await getattr(provider, stage)(frame())
    assert result.value == "她把门闩抬起来，看见他站在台阶下。"
    body = json.loads(seen[0]["messages"][1]["content"])
    assert body["method"]["version"] == "version-old"
    assert body["method"]["instruction"] == frame()["method"]["instruction"]
    assert body["sources"][0]["text"] == frame()["sources"][0]["text"]
    assert "private transport reasoning" not in result.value


@pytest.mark.parametrize("text", ["她看见node-a。", "她走出门。（来源：1）", "source_spans: 她走出门。", "她走出门。[source 1]", "她走出门。参考1", "她走出门。[1]", "她走出门。【1】", "她走出门。edge_id:1", "view_id:1 她走出门。"])
async def test_prose_technical_references_fail_with_safe_billed_feedback(text):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope({"text": text})))) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY), client)
        with pytest.raises(ProviderError) as captured:
            await provider.draft(frame())
    assert captured.value.code == "MODEL_OUTPUT_INVALID"
    assert captured.value.usage == (81, 23)
    assert captured.value.feedback.category == "PROSE_INVALID"


async def test_context_capacity_preserves_feedback_and_returns_recoverable_before_transport():
    calls = []

    def forbidden(request):
        calls.append(request)
        raise AssertionError("Capacity preflight must happen before transport")

    current = frame()
    current["responses"] = [{"id": "response-long", "text": "反馈的末尾也需要保留。" * 60_000}]
    original = copy.deepcopy(current)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY, context_target_chars=18_000), client)
        with pytest.raises(ProviderError) as captured:
            await provider.decide(current)
    assert captured.value.code == "CONTEXT_TOO_LARGE"
    assert captured.value.retryable
    assert captured.value.usage is None
    assert current == original
    assert not calls


async def test_soft_page_target_keeps_intact_feedback_when_transport_capacity_allows():
    current = frame()
    message = "这一判断还要在后续试写里检验。" * 1800
    current["responses"] = [{"id": "response-long", "text": message}]
    received = []
    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"action": "wait", "args": {}}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = DeepSeekGrowthProvider(Settings(api_key=KEY, context_target_chars=18_000), client)
        await provider.decide(current)
    actual = received[0]["messages"]
    assert sum(len(item["content"]) for item in actual) > 18_000
    assert json.loads(actual[1]["content"])["responses"][0]["text"] == message


@pytest.mark.parametrize("change", ["missing_sources", "preview_only", "missing_version", "blank_method"])
def test_chosen_prose_material_cannot_be_replaced_by_unread_catalog_preview(change):
    current = frame()
    if change == "missing_sources":
        current["sources"] = []
    elif change == "preview_only":
        current["sources"][0].pop("text")
    elif change == "missing_version":
        current["method"].pop("version")
    else:
        current["method"]["instruction"] = ""
    with pytest.raises(ProviderError):
        project_frame(current, "draft")


async def test_secret_echo_and_raw_http_error_never_cross_safe_boundary():
    for response in [httpx.Response(500, text=f"raw-server-error-{KEY}"),
                     httpx.Response(200, json=envelope({"text": f"echo {KEY}"}))]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            with pytest.raises(ProviderError) as captured:
                await DeepSeekGrowthProvider(Settings(api_key=KEY), client).draft(frame())
        safe = str(captured.value) + json.dumps(captured.value.public())
        assert KEY not in safe
        assert "raw-server-error" not in safe
        assert captured.value.__cause__ is None


def test_factory_no_new_provider_or_dependency():
    assert isinstance(make_growth_provider("fixture", Settings()), FixtureGrowthProvider)
    assert isinstance(make_growth_provider("deepseek", Settings()), DeepSeekGrowthProvider)
    with pytest.raises(ProviderError):
        make_growth_provider("unknown", Settings())


def test_projection_cannot_disclose_credential_embedded_in_configured_model():
    provider = DeepSeekGrowthProvider(Settings(api_key=KEY, model=f"model-{KEY}"))
    with pytest.raises(ProviderError) as captured:
        provider.projection(frame(), "decide")
    assert KEY not in str(captured.value)


def test_recursive_method_program_and_explicit_reflection_use_exact_delivered_versions():
    current = frame()
    current["allowed_actions"].append("reflect")
    current["responses"] = [{"id": "review", "version": "review-v", "text": "比较两种选材的后果。"}]
    method_ref = {"kind": "method", "object_id": "method-old", "version": "version-old"}
    review_ref = {"kind": "response", "object_id": "review", "version": "review-v"}
    program = {"inputs": [method_ref, review_ref], "calls": [
        {"method_ref": method_ref, "input_refs": [review_ref]},
    ], "node_ids": ["node-b"]}
    revision = {"action": "revise", "args": {
        "method_id": "method-old", "method_version": "version-old", "instruction": "比较选材方式留下的后果。",
        "input_refs": [review_ref], "program": program,
    }}
    assert validate_decision(current, revision) == revision
    reflection = {"action": "reflect", "args": {
        "method_id": "method-old", "about_refs": [method_ref, review_ref], "purpose": "检验上次判断的标准",
    }}
    assert validate_decision(current, reflection) == reflection
    assert validate_decision(current, {"action": "reflect", "args": {"method_id": "method-old"}})
    body = DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    assert '"calls"' in body["messages"][0]["content"]
    assert '"$defs"' in body["messages"][0]["content"]
    assert "方法版本继承表示沿革" not in body["messages"][0]["content"]  # no fabricated observation


@pytest.mark.parametrize("field", ["inputs", "calls"])
def test_recursive_program_rejects_unseen_versions_and_private_method_ids_without_echo(field):
    current = frame()
    private = {"kind": "method", "object_id": "private-method", "version": "private-version"}
    program = {field: [private] if field == "inputs" else [{"method_ref": private}]}
    with pytest.raises(ProviderError) as error:
        validate_decision(current, {"action": "revise", "args": {
            "method_id": "method-old", "instruction": "组合实际方式。", "program": program,
        }})
    assert error.value.feedback.prompt_data()["issues"] == ["program:schema_invalid", "SCHEMA_INVALID"]
    assert "private" not in str(error.value)


def test_multi_version_handle_requires_exact_reference_or_explicit_method_version():
    current = frame()
    current["pages"] = [{"method": {**current["method"], "version": "older-version"}}]
    decision = {"action": "inspect", "args": {"method_id": "method-old"}}
    with pytest.raises(ProviderError) as error:
        validate_decision(current, decision)
    assert error.value.feedback.prompt_data()["issues"] == ["method_id:schema_invalid", "HANDLE_AMBIGUOUS"]
    decision["args"]["method_version"] = "older-version"
    assert validate_decision(current, decision) == decision
    reading = {"action": "read_page", "args": {
        "kind": "method", "id": "method-old", "version": "older-version",
    }}
    assert validate_decision(current, reading) == reading


def test_method_cache_and_executed_plan_offer_deep_exact_method_handles():
    current = frame()
    nested = {"id": "method-deep", "version": "deep-v", "instruction": "从评议标准选择新的材料。"}
    current["method_cache"] = [nested]
    current["method_execution"] = {"instruction_refs": [{"method_ref": {
        "kind": "method", "object_id": "method-deep", "version": "deep-v",
    }}], "max_call_depth": 3}
    current["iteration_work"] = {"unchanged_actions": ["inspect"], "recent_no_change_count": 2,
                                 "method_review_due": True, "formal_review_order": 3,
                                 "last_review_ref": {"kind": "response", "object_id": "review", "version": "rv"}}
    assert validate_decision(current, {"action": "apply", "args": {"method_id": "method-deep"}})
    projected = DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    system = projected["messages"][0]["content"]
    data = json.loads(projected["messages"][1]["content"])
    assert data["method_cache"] == [nested]
    assert "复用 method_cache" in system
    assert "方法版本继承表示沿革" in system


async def test_review_is_public_positive_work_product_not_hidden_reasoning_or_narrative_event():
    current = frame()
    current["sources"] = []
    current["method_evidence"] = [{"kind": "method", "id": "method-old", "version": "version-old",
                                   "instruction": current["method"]["instruction"]}]
    current["method_execution"] = {"max_call_depth": 2}
    text = "这次选择让门内的回应进入下一步。把两人的去处共同作为选材，再检验实际行动留下的差异。"
    received = []
    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"text": text}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DeepSeekGrowthProvider(Settings(api_key=KEY), client).review(current)
    assert result.value == text and len(received) == 1
    system = received[0]["messages"][0]["content"]
    data = json.loads(received[0]["messages"][1]["content"])
    assert "完整公开工作判断及可执行下一步" in system
    assert "正向、直接" in system
    assert data["method_evidence"] == current["method_evidence"]
    assert data["method_execution"] == current["method_execution"]
    assert "private transport reasoning" not in str(result.value)
    assert await FixtureGrowthProvider().review(current)


def test_publish_and_response_can_choose_exact_versions_from_history():
    current = frame()
    current["trials"] = [{"id": "trial", "version": "current-trial"}]
    current["method_dependencies"] = [{"kind": "trial", "object_id": "trial", "version": "previous-trial"},
                                      {"kind": "method", "object_id": "method-old", "version": "past-method"}]
    publication = {"action": "publish", "args": {"trial_id": "trial"}}
    with pytest.raises(ProviderError) as error:
        validate_decision(current, publication)
    assert "HANDLE_AMBIGUOUS" in error.value.feedback.issues
    publication["args"]["trial_version"] = "current-trial"
    assert validate_decision(current, publication) == publication
    response = {"action": "respond", "args": {
        "text": "比较反馈更新前后的选材效果。", "evaluation_method_id": "method-old",
        "evaluation_method_version": "past-method", "about_refs": [
            {"kind": "trial", "object_id": "trial", "version": "previous-trial"}],
    }}
    assert validate_decision(current, response) == response
    response["args"]["evaluation_method_version"] = "unseen-version"
    with pytest.raises(ProviderError) as error:
        validate_decision(current, response)
    assert "HANDLE_VERSION_MISMATCH" in error.value.feedback.issues
    assert "unseen-version" not in str(error.value)


def test_review_and_render_receive_protocol_and_exact_work_evidence():
    current = frame("reading")
    current.update(work_protocol="growth-work-v2", purpose="检验选择方法本身", review_order=4,
                   review_targets=[{"kind": "method", "object_id": "method-old", "version": "version-old"}],
                   method_evidence=[{"kind": "method", "id": "method-old", "version": "version-old",
                                     "instruction": current["method"]["instruction"]}])
    reviewed = project_frame(current, "review")
    assert reviewed["review_order"] == 4
    assert reviewed["review_targets"] == current["review_targets"]
    assert reviewed["purpose"] == current["purpose"]
    rendered = project_frame(current, "render")
    assert rendered["work_protocol"] == "growth-work-v2"
    assert rendered["method_evidence"] == current["method_evidence"]


@pytest.mark.parametrize("actions", [None, [], ["unknown"], [True], {"wait": True}])
def test_invalid_frozen_capability_table_is_safe_frame_error(actions):
    current = frame()
    current["allowed_actions"] = actions
    with pytest.raises(ProviderError) as captured:
        project_frame(current, "decide")
    assert captured.value.code == "GROWTH_FRAME_INVALID"


@pytest.mark.parametrize("field", ["method_work", "reading_work"])
def test_invalid_work_observation_is_safe_frame_error(field):
    current = frame()
    current[field] = ["untrusted diagnostic data"]
    with pytest.raises(ProviderError) as captured:
        DeepSeekGrowthProvider(Settings()).projection(current, "decide")
    assert captured.value.code == "GROWTH_FRAME_INVALID"
    assert "untrusted" not in str(captured.value)
