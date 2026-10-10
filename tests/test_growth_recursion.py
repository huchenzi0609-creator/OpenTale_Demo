"""Offline execution checks for typed, higher-order method composition."""

from __future__ import annotations

import json

import pytest
from opentale.growth_kernel import GrowthError, add_node
from opentale.growth_methods import compile_method
from opentale.growth_store import GrowthStore, digest
from opentale.growth_types import GraphState, MethodVersion, ObjectRef, WindowState


@pytest.fixture
def graph(tmp_path):
    store = GrowthStore(tmp_path)
    state = GraphState(run_id="compositions", windows={"a": WindowState(id="a"), "b": WindowState(id="b")})
    store.create(state)
    state = add_node(store, state, "a", "她搬开椅子，让他走到门边。", request_id="opening")
    state = add_node(store, state, "b", "他将借来的钥匙留在窗台上。", request_id="response")
    yield store, state
    store.close()


def object_ref(kind, row):
    return ObjectRef(kind=kind, object_id=row["id"], version=row["version"])


def method(store, state, key, instruction, program=None, **kwargs):
    row = store.put(state.run_id, "method", MethodVersion(
        id=key, scope_id="window:a", private_scope="window:a", instruction=instruction,
        program=program, created_commit=state.graph_commit, **kwargs,
    ))
    return object_ref("method", row)


def test_composed_methods_execute_child_selection_and_keep_method_inputs_typed(graph):
    store, state = graph
    opening, answer = [state.windows[key].working_node_id for key in ("a", "b")]
    inner = method(store, state, "practical-consequence", "把钥匙的交付与门边的停顿组织成一次选择。", {
        "node_ids": [answer], "groups": {"后果": [answer]},
        "relations": [{"source_id": opening, "target_id": answer, "relation": "生活的承接"}],
    })
    response = store.put(state.run_id, "response", {
        "id": "review-selection", "scope_id": "window:a", "private_scope": "window:a",
        "text_ref": store.put_text("旧方法总把感受放在行动前面，改从钥匙被放下后的责任进入。").model_dump(),
    })
    response_ref = object_ref("response", response)
    outer = method(store, state, "review-the-selection", "重新评议选材方式，让交付改变后续关系。", {
        "inputs": [inner.model_dump(), response_ref.model_dump()],
        "calls": [{"method_ref": inner.model_dump()}],
        "node_ids": [opening], "groups": {"来处": [opening]},
    })

    view = compile_method(store, state, "a", outer, [])

    assert {ref.object_id for ref in view.members} == {opening, answer}
    assert all(ref.kind == "node" for ref in view.members)
    assert view.groups["后果"][0].object_id == answer
    assert view.groups["来处"][0].object_id == opening
    assert view.relations[0]["source_ref"]["object_id"] == opening
    assert inner in view.input_refs and response_ref in view.input_refs
    assert view.execution["max_call_depth"] == 1
    assert view.execution["method_as_data_refs"] == [inner.model_dump()]
    assert [frame["method_ref"] for frame in view.execution["application_frames"]] == [
        inner.model_dump(), outer.model_dump(),
    ]
    assert view.execution["status"] == "completed"
    # Definitions and reviews remain retrievable exact objects, never copied
    # into a world node or recursively duplicated inside the compiled view.
    serialized = json.dumps(view.model_dump(), ensure_ascii=False)
    assert store.read_text(response["text_ref"]) not in serialized
    assert "她搬开椅子" not in serialized


def test_exact_call_versions_survive_a_later_method_revision(graph):
    store, state = graph
    first = state.windows["a"].working_node_id
    second = state.windows["b"].working_node_id
    historical = method(store, state, "criterion", "从让出的位置看关系。", {"node_ids": [first]})
    composed = method(store, state, "application", "观察此选法导致的行动。", {
        "calls": [{"method_ref": historical.model_dump()}],
    })
    later = method(store, state, "criterion", "从失去的钥匙看关系。", {"node_ids": [second]})
    assert historical.version != later.version

    view = compile_method(store, state, "a", composed, [])

    assert [ref.object_id for ref in view.members] == [first]
    assert view.execution["instruction_refs"][0]["method_ref"] == historical.model_dump()
    assert view.execution["instruction_refs"][0]["instruction_preview"] == "从让出的位置看关系。"
    assert later not in view.input_refs
    parent_frame = view.execution["application_frames"][-1]
    assert parent_frame["output_node_refs"][0]["object_id"] == first
    assert parent_frame["input_refs"][0]["object_id"] == first


def test_root_explicit_selection_can_discard_child_result_while_default_inherits_it(graph):
    store, state = graph
    first = state.windows["a"].working_node_id
    inner = method(store, state, "child-selection", "取出交付后的实际变化。", {"node_ids": [first]})
    calls = [{"method_ref": inner.model_dump()}]
    inherited = method(store, state, "inherit-output", "用子方法的结果继续组织。", {"calls": calls})
    explicit = method(store, state, "discard-output", "改从其他方向选择材料。", {"calls": calls, "node_ids": []})

    default_view = compile_method(store, state, "a", inherited, [])
    explicit_view = compile_method(store, state, "a", explicit, [])

    # The entire executed organization is still inspectable. Only the root's
    # actual output defines the selected sources of the next narrative call.
    assert [r.object_id for r in explicit_view.members] == [first]
    assert [r["object_id"] for r in default_view.execution["root_output_refs"]] == [first]
    assert explicit_view.execution["root_output_refs"] == []
    assert explicit_view.execution["application_frames"][-1]["output_node_refs"] == []


def test_parent_history_does_not_invent_a_recursive_execution_or_reinject_old_instruction(graph):
    store, state = graph
    initial = method(store, state, "initial", "从已构造的生活中发现值得继续的方向。")
    newer = method(store, state, "changed", "让一次交付改变谁承担责任。", {"inputs": []},
                   parent_ref=initial, source_refs=[initial])

    view = compile_method(store, state, "a", newer, [])

    assert view.execution["max_call_depth"] == 0
    assert len(view.execution["application_frames"]) == 1
    assert view.execution["method_as_data_refs"] == []
    assert initial not in view.input_refs
    assert "从已构造的生活" not in json.dumps(view.execution, ensure_ascii=False)


def test_composition_uses_an_explicit_stack_beyond_python_recursion_depth(graph):
    store, state = graph
    first = state.windows["a"].working_node_id
    current = method(store, state, "base", "读交付之后的变化。", {"node_ids": [first]})
    for index in range(1050):
        current = method(store, state, f"layer-{index}", "重新组织上一方法的输入。", {
            "inputs": [current.model_dump()],
            "calls": [{"method_ref": current.model_dump()}],
        })

    view = compile_method(store, state, "a", current, [])

    assert view.execution["max_call_depth"] == 1050
    assert len(view.execution["application_frames"]) == 1051
    assert len(view.execution["method_as_data_refs"]) == 1050
    assert [ref.object_id for ref in view.members] == [first]
    assert view.execution["suspended"] == []


def test_dense_identical_invocations_share_results_without_exponential_expansion(graph):
    store, state = graph
    first = state.windows["a"].working_node_id
    current = method(store, state, "base", "观察交付之后的变化。", {"node_ids": [first]})
    for index in range(50):
        current = method(store, state, f"diamond-{index}", "让不同来处共同作用于同一次选择。", {
            "calls": [{"method_ref": current.model_dump()}, {"method_ref": current.model_dump()}],
        })

    view = compile_method(store, state, "a", current, [])

    frames = view.execution["application_frames"]
    assert view.execution["max_call_depth"] == 50
    assert len(frames) == 101
    assert sum(frame["status"] == "reused" for frame in frames) == 50
    assert len(view.execution["instruction_refs"]) == 51
    assert [ref.object_id for ref in view.members] == [first]


def test_cycle_from_an_imported_adapter_suspends_a_frame_instead_of_reentering(graph, monkeypatch):
    store, state = graph
    current = method(store, state, "loop", "评议当前方法如何选择。", {"inputs": []})
    original_get = store.get

    def cyclic_get(run_id, kind, object_id, **kwargs):
        row = original_get(run_id, kind, object_id, **kwargs)
        # An adversarial imported adapter can expose a cycle that the normal
        # content-addressed chronological store never constructs itself.
        if row and kind == "method" and object_id == current.object_id:
            row["program"] = {"calls": [{"method_ref": current.model_dump()}]}
        return row

    monkeypatch.setattr(store, "get", cyclic_get)
    view = compile_method(store, state, "a", current, [])

    assert view.execution["status"] == "suspended"
    assert view.execution["max_call_depth"] == 1
    continuation = view.execution["suspended"][0]
    assert continuation["method_ref"] == current.model_dump()
    assert continuation["reason"] == "active_method_cycle"
    assert len(view.execution["application_frames"]) == 2
    assert view.execution["root_output_refs"] == []


@pytest.mark.parametrize("location", ["inputs", "call_method", "call_inputs"])
def test_composition_never_reads_another_scopes_private_material(graph, location):
    store, state = graph
    hidden = store.put(state.run_id, "method", MethodVersion(
        id="private-other", scope_id="window:b", private_scope="window:b", instruction="只属于乙的内容。",
    ))
    hidden_ref = object_ref("method", hidden)
    available = method(store, state, "available", "用公开行动选材。")
    program = {
        "inputs": {"inputs": [hidden_ref.model_dump()]},
        "call_method": {"calls": [{"method_ref": hidden_ref.model_dump()}]},
        "call_inputs": {"calls": [{"method_ref": available.model_dump(), "input_refs": [hidden_ref.model_dump()]}]},
    }[location]
    caller = method(store, state, "caller", "应用一项具体组织。", program)

    with pytest.raises(GrowthError) as caught:
        compile_method(store, state, "a", caller, [])

    assert caught.value.code in {"METHOD_SOURCE_UNAVAILABLE", "METHOD_UNAVAILABLE"}
    assert store.list(state.run_id, "view", scope_id="window:a") == []


def test_existing_language_three_view_keeps_its_original_hash_and_stored_shape(graph):
    store, state = graph
    first = state.windows["a"].working_node_id
    current = method(store, state, "legacy", "观察这次交付。", {"node_ids": [first]})
    view = compile_method(store, state, "a", current, [])
    stored = store.get(state.run_id, "view", view.id, version=view.version, scope_id="window:a")
    expected_id = "view-" + digest({
        "scope": "window:a", "method": current.model_dump(),
        "members": [ref.model_dump() for ref in view.members], "groups": {}, "relations": [], "occurrences": [],
    })[:24]

    assert view.id == expected_id
    assert "input_refs" not in stored and "execution" not in stored
    assert stored["version"] == digest({key: value for key, value in stored.items() if key != "version"})
    assert view.input_refs == [] and view.execution == {}


@pytest.mark.parametrize("program", [
    {"inputs": ["unknown"]}, {"inputs": [{"kind": "event", "object_id": "event", "version": "v"}]},
    {"calls": [{"method_ref": {"kind": "node", "object_id": "node", "version": "v"}}]},
    {"calls": [{"method_ref": "self"}]}, {"calls": {}},
])
def test_composition_rejects_invalid_or_executable_program_values(graph, program):
    store, state = graph
    current = method(store, state, "invalid", "执行安全的声明式组合。", program)
    with pytest.raises(GrowthError) as caught:
        compile_method(store, state, "a", current, [])
    assert caught.value.code in {"METHOD_PROGRAM", "METHOD_REF_KIND"}
