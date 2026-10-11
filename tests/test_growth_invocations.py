"""Actual model-application plans are exact, finite and separate from fiction."""

from __future__ import annotations

import json

import pytest
from opentale.growth_invocations import attach_invocation_result, invocation_frame, plan_invocations
from opentale.growth_kernel import GrowthError, add_node
from opentale.growth_store import GrowthStore
from opentale.growth_types import GraphState, MethodVersion, ObjectRef, WindowState


@pytest.fixture
def graph(tmp_path):
    store = GrowthStore(tmp_path)
    state = GraphState(run_id="actual-method-invocations", windows={"a": WindowState(id="a"), "b": WindowState(id="b")})
    store.create(state)
    state = add_node(store, state, "a", "她将钥匙交给了邻居。", request_id="start")
    state = add_node(store, state, "b", "邻居打算午后出门，桌上摊着车站的时刻表。", request_id="answer")
    yield store, state
    store.close()


def ref(kind, row):
    return ObjectRef(kind=kind, object_id=row["id"], version=row["version"])


def node(store, state, key="a"):
    return ref("node", store.get(state.run_id, "node", state.windows[key].working_node_id))


def method(store, state, key, instruction="从新交付的钥匙产生的责任继续构造。", program=None, **kwargs):
    row = store.put(state.run_id, "method", MethodVersion(id=key, instruction=instruction,
        program=program, scope_id="window:a", private_scope="window:a", **kwargs))
    return ref("method", row)


def result(store, state, frame, text, children=None, used_source_spans=None):
    return store.put(state.run_id, "invocation", {"id": frame["frame_id"],
        "scope_id": "window:a", "private_scope": "window:a", "method_ref": frame["method_ref"],
        "fingerprint": frame["fingerprint"], "input_refs": frame["input_refs"],
        "text_ref": store.put_text(text).model_dump(), "child_result_refs": children or [],
        "used_source_spans": used_source_spans or [], "request_id": "offline-completed-invoke"})


def test_plain_method_has_real_root_application_without_executing_ancestry(graph):
    store, state = graph
    old = method(store, state, "old", "反复描写桌面的旧式样。")
    active = method(store, state, "current", parent_ref=old, source_refs=[old])
    opening = node(store, state)
    plan = plan_invocations(store, state, "a", active, [opening], context={"mode": "draft", "goal": "午后生活"})
    assert len(plan["frames"]) == 1
    assert plan["max_call_depth"] == 0
    assert plan["status"] == "ready"
    invocation = invocation_frame(store, state, "a", plan, plan["root_frame_id"], {},
        base_frame={"sources": [{"id": opening.object_id, "version": opening.version, "text": "她将钥匙交给了邻居。"}]})
    assert invocation["method"]["instruction"] == "从新交付的钥匙产生的责任继续构造。"
    assert "反复描写桌面的旧式样" not in json.dumps(invocation, ensure_ascii=False)
    assert invocation["invocation_children"] == []
    assert invocation["invocation_context"]["mode"] == "draft"
    assert store.count_objects(state.run_id, "invocation") == 0


def test_child_model_product_reaches_caller_and_saved_root_reaches_actual_generation(graph):
    store, state = graph
    neighbor = node(store, state, "b")
    child = method(store, state, "child", "考察车站时刻表怎样打开另一段生活。", {"node_ids": [neighbor.object_id]})
    root = method(store, state, "root", "让交付影响另一人的午后。", {
        "calls": [{"method_ref": child.model_dump()}],
    })
    plan = plan_invocations(store, state, "a", root, [node(store, state)])
    child_frame, root_frame = plan["frames"]
    assert child_frame["depth"] == 1 and root_frame["depth"] == 0
    with pytest.raises(GrowthError) as caught:
        invocation_frame(store, state, "a", plan, root_frame["frame_id"], {})
    assert caught.value.code == "METHOD_INVOCATION_REQUIRED"

    child_result = result(store, state, child_frame, "把邻居按时出门作为分岔；钥匙交付使她替他接待访客。")
    results = {child_frame["frame_id"]: child_result}
    caller_input = invocation_frame(store, state, "a", plan, root_frame["frame_id"], results)
    assert caller_input["invocation_children"][0]["text"].startswith("把邻居按时出门")
    assert caller_input["invocation_children"][0]["status"] == "proposal_not_world_fact"
    assert neighbor.model_dump() in root_frame["input_refs"]

    child_ref = ref("invocation", child_result).model_dump()
    results[root_frame["frame_id"]] = result(store, state, root_frame,
        "让她替出门的邻居打开门，访客将一次交付带往新的关系。", [child_ref])
    before = store.count_objects(state.run_id, "node")
    draft = attach_invocation_result(store, state, "a", plan, results, {"parent_ids": [node(store, state).object_id]})
    assert draft["method_invocation"]["text"].startswith("让她替出门的邻居打开门")
    assert draft["method_invocation"]["child_result_refs"] == [child_ref]
    assert draft["method_invocation"]["max_call_depth"] == 1
    assert store.count_objects(state.run_id, "node") == before


def test_task_and_exact_input_changes_invalidate_cache_but_unrelated_commits_do_not(graph):
    store, state = graph
    active = method(store, state, "active")
    opening = node(store, state)
    initial = plan_invocations(store, state, "a", active, [opening], context={"mode": "draft", "goal": "开门"})
    state = add_node(store, state, "b", "车站售票窗开了。", request_id="later")
    identical = plan_invocations(store, state, "a", active, [opening], context={"mode": "draft", "goal": "开门"})
    assert identical["frames"][0]["fingerprint"] == initial["frames"][0]["fingerprint"]
    changed = plan_invocations(store, state, "a", active, [opening], context={"mode": "draft", "goal": "接待访客"})
    assert changed["frames"][0]["fingerprint"] != initial["frames"][0]["fingerprint"]
    new_inputs = plan_invocations(store, state, "a", active, [opening, node(store, state, "b")],
                                  context={"mode": "draft", "goal": "开门"})
    assert new_inputs["frames"][0]["fingerprint"] != initial["frames"][0]["fingerprint"]


def test_exact_historical_method_versions_and_typed_feedback_are_actual_call_materials(graph):
    store, state = graph
    historical = method(store, state, "criterion", "从交付以后谁需要出门来选材。")
    newer = method(store, state, "criterion", "从门边的静物来选材。")
    response = store.put(state.run_id, "response", {"id": "review", "private_scope": "window:a",
        "text_ref": store.put_text("改从访客的到达观察所用标准遗漏了什么。 ").model_dump()})
    feedback_ref = ref("response", response)
    root = method(store, state, "review-method", program={"inputs": [feedback_ref.model_dump()], "calls": [
        {"method_ref": historical.model_dump(), "input_refs": [feedback_ref.model_dump()]},
    ]})
    plan = plan_invocations(store, state, "a", root, [node(store, state)])
    child = plan["frames"][0]
    assert child["method_ref"] == historical.model_dump()
    frame = invocation_frame(store, state, "a", plan, child["frame_id"], {})
    assert frame["method"]["instruction"] == "从交付以后谁需要出门来选材。"
    assert frame["method_evidence"][0]["text"].startswith("改从访客")
    assert newer.model_dump() not in [f["method_ref"] for f in plan["frames"]]


def test_shared_diamond_executes_one_unique_child_for_same_inputs(graph):
    store, state = graph
    current = method(store, state, "base", program={"node_ids": [node(store, state).object_id]})
    for index in range(50):
        current = method(store, state, f"layer-{index}", program={"calls": [
            {"method_ref": current.model_dump()}, {"method_ref": current.model_dump()},
        ]})
    plan = plan_invocations(store, state, "a", current, [])
    assert len(plan["frames"]) == 51
    assert len(plan["applications"]) == 101
    assert sum(a["status"] == "reused" for a in plan["applications"]) == 50
    assert plan["max_call_depth"] == 50
    assert all(len(frame["child_frame_ids"]) <= 1 for frame in plan["frames"])


def test_finite_program_exceeds_python_recursion_limit_without_a_depth_ceiling(graph):
    store, state = graph
    current = method(store, state, "base")
    for index in range(1050):
        current = method(store, state, f"layer-{index}", program={"calls": [{"method_ref": current.model_dump()}]})
    plan = plan_invocations(store, state, "a", current, [])
    assert len(plan["frames"]) == 1051
    assert plan["max_call_depth"] == 1050
    assert plan["status"] == "ready"


def test_private_inputs_and_invocation_results_cannot_cross_scope(graph):
    store, state = graph
    hidden = store.put(state.run_id, "response", {"id": "private", "private_scope": "window:b",
        "text_ref": store.put_text("别人的私有评议").model_dump()})
    active = method(store, state, "active")
    with pytest.raises(GrowthError) as caught:
        plan_invocations(store, state, "a", active, [ref("response", hidden)])
    assert caught.value.code == "METHOD_SOURCE_UNAVAILABLE"
    plan = plan_invocations(store, state, "a", active, [node(store, state)])
    saved = result(store, state, plan["frames"][0], "打开另一个方向。")
    with pytest.raises(GrowthError):
        attach_invocation_result(store, state, "b", plan, {plan["root_frame_id"]: saved}, {})


def test_cached_result_must_match_frozen_method_and_evidence(graph):
    store, state = graph
    active = method(store, state, "active")
    plan = plan_invocations(store, state, "a", active, [node(store, state)])
    saved = result(store, state, plan["frames"][0], "让交付带往访客。")
    altered = {**saved, "fingerprint": "different-application"}
    with pytest.raises(GrowthError) as caught:
        attach_invocation_result(store, state, "a", plan, {plan["root_frame_id"]: altered}, {})
    assert caught.value.code == "METHOD_INVOCATION_STALE"


def test_render_invocation_preserves_visit_excerpt_without_expanding_unseen_source(graph):
    store, state = graph
    opening = node(store, state)
    active = method(store, state, "render-current")
    plan = plan_invocations(store, state, "a", active, [opening], context={"mode": "render"})
    supplied = {"sources": [{"id": opening.object_id, "version": opening.version,
                             "text": "钥匙", "start": 2, "end": 4}]}
    frame = invocation_frame(store, state, "a", plan, plan["root_frame_id"], {}, base_frame=supplied)
    assert frame["sources"] == supplied["sources"]
    assert "text" not in frame["method_evidence"][0]
    assert "她将钥匙交给了邻居" not in json.dumps(frame, ensure_ascii=False)


def test_child_draft_retrieves_exact_extra_node_but_render_cannot_expand_unvisited_material(graph):
    store, state = graph
    opening, extra = node(store, state), node(store, state, "b")
    child = method(store, state, "extra-material", program={"inputs": [extra.model_dump()]})
    root = method(store, state, "root-with-other-source", program={"calls": [{"method_ref": child.model_dump()}]})
    base = {"sources": [{"id": opening.object_id, "version": opening.version,
                         "text": "钥匙", "start": 2, "end": 4}]}
    drafting = plan_invocations(store, state, "a", root, [opening], context={"mode": "draft"})
    child_frame = invocation_frame(store, state, "a", drafting, drafting["frames"][0]["frame_id"], {}, base_frame=base)
    delivered = next(s for s in child_frame["sources"] if s["id"] == extra.object_id)
    assert delivered["text"] == "邻居打算午后出门，桌上摊着车站的时刻表。"
    assert delivered["start"] == 0 and delivered["end"] == len(delivered["text"])
    assert child_frame["sources"][0] == base["sources"][0]
    assert any(span["node_id"] == extra.object_id for span in child_frame["invocation_source_spans"])
    rendering = plan_invocations(store, state, "a", root, [opening], context={"mode": "render"})
    read_frame = invocation_frame(store, state, "a", rendering, rendering["frames"][0]["frame_id"], {}, base_frame=base)
    assert read_frame["sources"] == base["sources"]
    assert "时刻表" not in json.dumps(read_frame, ensure_ascii=False)


def test_actual_child_read_ranges_remain_provenance_when_root_selection_discards_node(graph):
    store, state = graph
    extra = node(store, state, "b")
    child = method(store, state, "read-then-discard", program={"inputs": [extra.model_dump()], "node_ids": []})
    root = method(store, state, "caller", program={"calls": [{"method_ref": child.model_dump()}], "node_ids": []})
    plan = plan_invocations(store, state, "a", root, [], context={"mode": "draft"})
    child_plan, root_plan = plan["frames"]
    child_frame = invocation_frame(store, state, "a", plan, child_plan["frame_id"], {})
    child_saved = result(store, state, child_plan, "从午后出门带来的责任开始。",
                         used_source_spans=child_frame["invocation_source_spans"])
    results = {child_plan["frame_id"]: child_saved}
    root_frame = invocation_frame(store, state, "a", plan, root_plan["frame_id"], results)
    assert root_frame["sources"] == []
    assert root_frame["invocation_source_spans"] == child_frame["invocation_source_spans"]
    results[root_plan["frame_id"]] = result(store, state, root_plan, "让门前的访客改变下午的安排。",
        [ref("invocation", child_saved).model_dump()], root_frame["invocation_source_spans"])
    drafting = attach_invocation_result(store, state, "a", plan, results, {"sources": []})
    assert drafting["sources"][0]["id"] == extra.object_id
    assert drafting["sources"][0]["text"].startswith("邻居打算午后出门")
    assert drafting["method_invocation"]["used_source_spans"] == child_frame["invocation_source_spans"]


def test_active_cycle_is_suspended_and_never_forwarded_to_generation(graph, monkeypatch):
    store, state = graph
    active = method(store, state, "cycle")
    original = store.get

    def cyclic(run_id, kind, object_id, **kwargs):
        row = original(run_id, kind, object_id, **kwargs)
        if row and kind == "method" and object_id == active.object_id:
            row["program"] = {"calls": [{"method_ref": active.model_dump()}]}
        return row

    monkeypatch.setattr(store, "get", cyclic)
    plan = plan_invocations(store, state, "a", active, [])
    assert plan["status"] == "suspended"
    assert plan["suspended"][0]["reason"] == "active_method_cycle"
    with pytest.raises(GrowthError) as caught:
        invocation_frame(store, state, "a", plan, plan["root_frame_id"], {})
    assert caught.value.code == "METHOD_INVOCATION_SUSPENDED"
