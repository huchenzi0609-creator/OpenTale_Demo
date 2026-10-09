"""Offline behavior checks for indexed growth, reading, and method language three."""

import json

import pytest
from opentale.growth_kernel import GrowthError, add_link, add_node, add_passage, commit_growth, navigate
from opentale.growth_methods import adopt_method, compile_method, revise_method
from opentale.growth_replay import (
    replay_operations,
    replay_range,
    save_checkpoint,
    save_head_checkpoint,
    verify_head_replay,
    verify_objects,
)
from opentale.growth_store import GrowthStore, canonical, digest
from opentale.growth_types import (
    GraphState,
    GrowthPatch,
    LinkDraft,
    MethodVersion,
    NodeDraft,
    ObjectRef,
    ReadingTrack,
    SourceSpan,
    WindowState,
)


@pytest.fixture
def graph(tmp_path):
    store = GrowthStore(tmp_path)
    state = GraphState(run_id="g", windows={"a": WindowState(id="a"), "b": WindowState(id="b")},
                       reading_tracks={"r1": ReadingTrack(id="r1"), "r2": ReadingTrack(id="r2")})
    store.create(state)
    yield store, state
    store.close()


def test_branches_multiple_parents_and_association_cycle(graph):
    store, initial = graph
    a = add_node(store, initial, "a", "甲看见了门。", request_id="a")
    a_id = a.windows["a"].working_node_id
    b = add_node(store, a, "b", "乙听见了水。", request_id="b")
    b_id = b.windows["b"].working_node_id
    branch = add_node(store, b, "a", "门外的另一条路。", parents=[a_id], request_id="branch")
    shared = add_node(store, branch, "b", "门与水在此交汇。", parents=[a_id, b_id], request_id="shared")
    shared_id = shared.windows["b"].working_node_id
    cycle = add_link(store, shared, shared_id, a_id, scope_id="a", relation="后来重新解释了门。")
    cycle = add_link(store, cycle, a_id, shared_id, scope_id="b", relation="门又指向水。")
    links = store.list("g", "link")
    assert len([link for link in links if link["target_id"] == shared_id and link["kind"] == "growth"]) == 2
    assert len([link for link in links if link["kind"] == "association"]) == 2
    assert cycle.graph_commit == 6
    assert initial.graph_commit == 0


def test_first_visit_can_follow_declared_entry_edge_without_rendering_entry(graph):
    store, state = graph
    state = add_node(store, state, "a", "这段开端可以不呈现。", request_id="entry")
    entry = state.windows["a"].working_node_id
    state = add_node(store, state, "a", "读者沿着它进入另一段生活。", parents=[entry], request_id="child")
    child = state.windows["a"].working_node_id
    state.reading_tracks["r1"].entry_node_ids = [entry]
    store.save(state)
    link = next(link for link in store.list("g", "link") if link["target_id"] == child)
    state = navigate(store, state, "r1", child, via_link_id=link["id"], transition="edge",
                     observed_graph_commit=state.graph_commit, request_id="first-hop")
    visit = store.list("g", "visit", scope_id="reading:r1")[0]
    assert visit["from_node_id"] == entry and visit["node_id"] == child
    assert state.reading_tracks["r1"].cursor_node_id == child
    assert state.reading_tracks["r1"].read_seq == 1
    assert not store.list("g", "passage", scope_id="reading:r1")


def test_same_patch_local_ids_atomicity_and_no_growth_cycle(graph):
    store, initial = graph
    patch = GrowthPatch(id="two", window_id="a", base_graph_commit=0,
                        nodes=[NodeDraft(local_id="x", text="甲。"), NodeDraft(local_id="y", text="乙。")],
                        links=[LinkDraft(source_id="x", target_id="y")])
    result = commit_growth(store, initial, patch)
    assert result.state.graph_commit == 1
    assert store.get("g", "link", result.link_ids[0])["source_id"] == result.local_ids["x"]
    invalid = GrowthPatch(id="invalid", window_id="a", base_graph_commit=1, nodes=patch.nodes,
                         links=[LinkDraft(source_id="y", target_id="x")])
    before = store.load("g").model_dump()
    with pytest.raises(GrowthError, match="较早"):
        commit_growth(store, result.state, invalid)
    assert store.load("g").model_dump() == before
    assert store.count_objects("g", "node") == 2


def test_input_dependencies_never_create_narrative_edges_and_idempotence(graph):
    store, initial = graph
    first = add_node(store, initial, "a", "她没有寄出信。", request_id="first")
    node = store.get("g", "node", first.windows["a"].working_node_id)
    ref = ObjectRef(kind="node", object_id=node["id"], version=node["version"])
    second = add_node(store, first, "b", "他以为信早已寄出。", input_refs=[ref], request_id="second")
    repeated = add_node(store, first, "b", "他以为信早已寄出。", input_refs=[ref], request_id="second")
    assert repeated.graph_commit == second.graph_commit == 2
    assert store.list("g", "link") == []
    assert len(store.list("g", "dependency", scope_id="b")) == 1
    assert store.list("g", "dependency", scope_id="a") == [store.get("g", "dependency", "first", scope_id="a")]
    with pytest.raises(GrowthError, match="幂等"):
        add_node(store, first, "b", "不同内容。", request_id="second")


def test_stale_global_base_allowed_but_local_scope_conflict_rejected(graph):
    store, initial = graph
    first = add_node(store, initial, "a", "甲。", request_id="first", expected_revision=0)
    other = add_node(store, initial, "b", "乙。", request_id="other", expected_revision=0)
    assert other.graph_commit == 2
    assert other.windows["a"].working_node_id == first.windows["a"].working_node_id
    with pytest.raises(GrowthError, match="改变"):
        add_node(store, initial, "a", "竞争结果。", expected_revision=0)
    assert store.load("g").graph_commit == 2


def test_cas_dedup_preserves_node_identity_and_exact_unicode_offsets(graph):
    store, initial = graph
    first = add_node(store, initial, "a", "甲😀乙\n", request_id="same-a")
    second = add_node(store, first, "b", "甲😀乙\n", request_id="same-b")
    nodes = store.list("g", "node")
    assert nodes[0]["id"] != nodes[1]["id"]
    assert nodes[0]["text_ref"] == nodes[1]["text_ref"]
    assert store.get_body(nodes[0]["text_ref"], 1, 2) == "😀"
    assert nodes[0]["text_ref"]["length"] == 4
    assert "甲😀乙" not in json.dumps(second.model_dump(), ensure_ascii=False)
    growth_events = [event for event in store.list_events("g") if event["kind"] == "growth"]
    assert all(event["payload"]["patch_format"] == "body-refs-v1" for event in growth_events)
    assert all("text" not in event["payload"]["patch"]["nodes"][0] for event in growth_events)


def test_scoped_private_nodes_and_links_do_not_leak(graph):
    store, initial = graph
    hidden = add_node(store, initial, "a", "她不愿公开的记忆。", private_scope="a", request_id="hidden")
    hidden_id = hidden.windows["a"].working_node_id
    visible = add_node(store, hidden, "a", "她望向远处。", parents=[hidden_id], request_id="public")
    assert len(store.list("g", "node")) == 0
    assert store.get("g", "node", hidden_id, scope_id="b") is None
    assert store.get("g", "node", hidden_id, scope_id="a") is not None
    assert store.list("g", "link") == []
    with pytest.raises(GrowthError, match="不可见"):
        add_node(store, visible, "b", "偷用秘密。", parents=[hidden_id])


def test_frozen_reading_revisits_and_incomplete_independent_tracks(graph):
    store, initial = graph
    first = add_node(store, initial, "a", "甲的起点。", request_id="first")
    entry = first.windows["a"].working_node_id
    next_state = add_node(store, first, "a", "甲的支路。", parents=[entry], request_id="next")
    next_id = next_state.windows["a"].working_node_id
    link = store.list("g", "link")[0]
    read = navigate(store, next_state, "r1", entry, observed_graph_commit=1, request_id="read1")
    assert read.graph_commit == 2 and read.reading_tracks["r1"].read_seq == 1
    with pytest.raises(GrowthError, match="不可见"):
        navigate(store, read, "r1", next_id, via_link_id=link["id"])
    read = navigate(store, read, "r1", next_id, via_link_id=link["id"], observed_graph_commit=2)
    read = navigate(store, read, "r1", entry, via_link_id=link["id"])
    assert read.reading_tracks["r1"].read_seq == 3
    assert read.reading_tracks["r2"].read_seq == 0
    assert [visit["node_id"] for visit in store.list("g", "visit", scope_id="reading:r1")] == [entry, next_id, entry]
    assert len(store.list("g", "node", at_commit=1)) == 1


def test_cross_entry_transition_is_track_only_and_passage_uses_actual_spans(graph):
    store, initial = graph
    first = add_node(store, initial, "a", "甲😀乙。", request_id="first")
    a_id = first.windows["a"].working_node_id
    second = add_node(store, first, "b", "另一入口。", request_id="second")
    b_id = second.windows["b"].working_node_id
    state = navigate(store, second, "r1", a_id, start=1, end=2, observed_graph_commit=2, request_id="v1")
    with pytest.raises(GrowthError, match="过渡"):
        navigate(store, state, "r1", b_id)
    state = navigate(store, state, "r1", b_id, transition="从门前转向水边。", request_id="v2")
    visits = store.list("g", "visit", scope_id="reading:r1")
    state = add_passage(store, state, "r1", "😀", visit_ids=[visits[0]["id"]], request_id="p1")
    assert state.graph_commit == 2 and state.reading_tracks["r1"].read_seq == 3
    assert store.list("g", "link") == []
    passage = store.list("g", "passage", scope_id="reading:r1")[0]
    assert passage["source_spans"][0]["start"] == 1
    node = store.get("g", "node", a_id)
    unvisited = SourceSpan(node_id=a_id, node_version=node["version"], text_ref=node["text_ref"], start=0, end=4)
    with pytest.raises(GrowthError, match="实际访问"):
        add_passage(store, state, "r1", "伪装读过全文。", visit_ids=[visits[0]["id"]], source_spans=[unvisited])


def test_method_versions_share_views_and_revision_actually_adopted(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲。", request_id="a")
    a_id = state.windows["a"].working_node_id
    state = add_node(store, state, "b", "乙。", request_id="b")
    b_id = state.windows["b"].working_node_id
    method = store.put("g", "method", MethodVersion(id="m", scope_id="a", instruction="并置。",
        program={"node_ids": [a_id, a_id, b_id], "groups": {"两处": [a_id, b_id]},
                 "relations": [{"source_id": a_id, "target_id": b_id, "relation": "并置"}]}))
    ref = ObjectRef(kind="method", object_id="m", version=method["version"])
    view = compile_method(store, state, "a", ref, [])
    assert len(view.members) == 2 and len(view.relations) == 1
    assert "text_ref" not in json.dumps(view.model_dump()) and "甲。" not in json.dumps(view.model_dump())
    new_ref = revise_method(store, state, "a", ref, instruction="先让沉默出现。")
    adopted = adopt_method(store, state, "a", new_ref)
    assert adopted.windows["a"].method_ref == new_ref
    assert new_ref.version != ref.version
    assert store.get("g", "method", "m", version=ref.version)["instruction"] == "并置。"
    assert store.get("g", "method", "m")["instruction"] == "先让沉默出现。"
    assert compile_method(store, adopted, "a", ref, []).version == view.version


def test_scope_prefixes_and_accepted_publication_recovery(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲。", request_id="accepted", expected_revision=0)
    state = add_node(store, state, "b", "乙。", request_id="other")
    recovered = add_node(store, state, "a", "甲。", request_id="accepted", expected_revision=1)
    assert recovered.graph_commit == 2 and store.count_objects("g", "node") == 2
    method = store.put("g", "method", MethodVersion(id="private-m", scope_id="window:a",
                                                   private_scope="window:a", instruction="静看。"))
    ref = ObjectRef(kind="method", object_id=method["id"], version=method["version"])
    assert store.get("g", "method", method["id"], scope_id="a") == method
    assert store.get("g", "method", method["id"], scope_id="window:b") is None
    assert compile_method(store, state, "window:a", ref, []).scope_id == "window:a"
    assert adopt_method(store, state, "window:a", ref).windows["a"].method_ref == ref


def test_bounded_replay_receipts_distinguish_range_from_full_semantics(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲😀。", request_id="first")
    state = add_node(store, state, "b", "乙。", request_id="second")
    node_id = state.windows["a"].working_node_id
    state = navigate(store, state, "r1", node_id, observed_graph_commit=2, request_id="v1")
    visit = store.list("g", "visit", scope_id="reading:r1")[0]
    add_passage(store, state, "r1", "甲😀。", visit_ids=[visit["id"]], request_id="p1")
    receipt = replay_range(store, "g", limit=2)
    assert receipt["events_checked"] == 2 and receipt["has_more"]
    assert receipt["base_verified"] and not receipt["full_event_history"]
    checkpoint = save_checkpoint(store, "g", receipt)
    tail = replay_range(store, "g", after_seq=receipt["through_seq"], checkpoint_ref=checkpoint)
    assert tail["base_verified"] and not tail["has_more"]
    assert not tail["full_event_history"] and not tail["semantic_replay"]
    objects = verify_objects(store, "g", limit=2)
    assert objects["objects_checked"] == 2 and objects["has_more"]
    assert not objects["full_object_history"]
    cursor = objects["through_seq"]
    while objects["has_more"]:
        objects = verify_objects(store, "g", after_seq=cursor, limit=2)
        cursor = objects["through_seq"]
    assert not objects["semantic_replay"]


def test_lightweight_heads_and_lists_survive_store_reopen(graph, tmp_path):
    store, initial = graph
    state = initial
    for index in range(25):
        state = add_node(store, state, "a", f"{index}" * 1000, request_id=f"n{index}")
    assert len(json.dumps(store.load("g").model_dump())) < 12_000
    assert "windows" not in store.list_runs()[0]
    assert len(store.list("g", "node", offset=5, limit=3)) == 3
    reopened = GrowthStore(tmp_path)
    assert reopened.load("g").graph_commit == 25
    assert reopened.count_objects("g", "node") == 25
    reopened.close()


def test_latest_track_pages_are_private_and_do_not_starve_after_100_visits(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "同一处可以反复进入。", request_id="seed")
    node_id = state.windows["a"].working_node_id
    for index in range(105):
        for track in ("r1", "r2"):
            state = navigate(store, state, track, node_id, observed_graph_commit=1,
                             request_id=f"{track}:{index}")
    assert store.list("g", "visit") == []
    latest = store.list("g", "visit", scope_id="reading:r1", latest_first=True, limit=3)
    assert [visit["read_seq"] for visit in latest] == [105, 104, 103]
    assert all(visit["private_scope"] == "reading:r1" for visit in latest)
    state = add_passage(store, state, "r1", "同一处。", visit_ids=[latest[0]["id"]])
    assert store.list("g", "passage", scope_id="reading:r2") == []
    assert store.list("g", "passage", scope_id="reading:r1")[0]["private_scope"] == "reading:r1"
    assert state.reading_tracks["r1"].read_seq == 106


def test_fork_and_delete_preserve_shared_cas_and_explicit_lineage(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "共同的一段原文。", request_id="seed")
    node_id = state.windows["a"].working_node_id
    source_node = store.get("g", "node", node_id)
    fork = store.fork_run("g", "fork", title="另一条路")
    assert fork.graph_commit == state.graph_commit and fork.title == "另一条路"
    assert store.get("fork", "node", node_id) == source_node
    assert [event["kind"] for event in store.list_events("fork")] == ["head_initial", "fork"]
    assert fork.config["fork_source"]["run_id"] == "g"
    fork = add_node(store, fork, "b", "另一条路延续了原文。", parents=[node_id], request_id="fork-growth")
    assert store.load("g").graph_commit == 1 and fork.graph_commit == 2
    assert store.delete_run("g") and not store.delete_run("g")
    assert store.load("g") is None and store.get_body(source_node["text_ref"]) == "共同的一段原文。"
    assert store.get("fork", "node", node_id) == source_node
    replay = replay_operations(store, "fork", page_size=2)
    assert replay["baseline_graph_commit"] == 1 and replay["operations_replayed"]["growth"] == 1


def test_resumable_head_reconstruction_and_checkpoint_validation(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "门前。", request_id="seed")
    node_id = state.windows["a"].working_node_id
    state = navigate(store, state, "r1", node_id, observed_graph_commit=1)
    state.windows["a"].direction = "沿尚未打开的门继续。"
    store.save(state)
    receipt = verify_head_replay(store, "g", limit=2)
    assert receipt["has_more"] and receipt["current_head_matches"] is None
    assert receipt["head"]["graph_commit"] == 1
    while receipt["has_more"]:
        checkpoint = save_head_checkpoint(store, "g", receipt)
        receipt = verify_head_replay(store, "g", after_seq=receipt["through_seq"],
                                     limit=2, checkpoint_ref=checkpoint)
    assert receipt["current_head_matches"] and receipt["head"] == store.load("g").model_dump(mode="json")
    assert not receipt["operational_replay"] and not receipt["semantic_replay"]
    forged = {**receipt, "head_hash": "incorrect"}
    with pytest.raises(GrowthError, match="重新校验"):
        save_head_checkpoint(store, "g", forged)


def test_deterministic_operation_replay_executes_graph_and_reading_kernel(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲😀在门前。", request_id="a")
    a_id = state.windows["a"].working_node_id
    state = add_node(store, state, "b", "乙在河边。", request_id="b")
    b_id = state.windows["b"].working_node_id
    state = add_node(store, state, "a", "两处声音在风里交汇。", parents=[a_id, b_id], request_id="merge")
    merged = state.windows["a"].working_node_id
    state = add_link(store, state, merged, a_id, scope_id="window:a", request_id="reinterpret")
    state = navigate(store, state, "r1", a_id, observed_graph_commit=4, start=1, end=2, request_id="visit")
    visit = store.list("g", "visit", scope_id="reading:r1")[0]
    state = add_passage(store, state, "r1", "😀", visit_ids=[visit["id"]], request_id="passage")
    method = store.put("g", "method", MethodVersion(id="m", scope_id="window:a",
                                                   private_scope="window:a", instruction="留意下一步。"))
    state = adopt_method(store, state, "window:a", ObjectRef(kind="method", object_id="m", version=method["version"]))
    receipt = replay_operations(store, "g", page_size=2)
    assert receipt["current_head_matches"] and receipt["operational_replay"]
    assert receipt["operations_replayed"] == {"growth": 3, "association": 1, "visit": 1, "passage": 1, "adopt_method": 1}
    assert not receipt["semantic_replay"]


def test_operation_replay_detects_rehashed_object_that_integrity_alone_accepts(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲。", request_id="a")
    a_id = state.windows["a"].working_node_id
    state = add_node(store, state, "b", "乙。", parents=[a_id], request_id="b")
    original = store.list("g", "link")[0]
    tampered = {**original, "relation": "篡改的声明，虽然对象哈希已经重算。"}
    tampered.pop("version")
    tampered["version"] = digest(tampered)
    with store.transaction():
        store._db.execute("UPDATE objects SET version=?,value_json=? WHERE run_id=? AND kind='link' AND object_id=?",
                          (tampered["version"], canonical(tampered), "g", original["id"]))
    assert verify_objects(store, "g", limit=100)["full_object_history"]
    with pytest.raises(GrowthError, match="重放结果"):
        replay_operations(store, "g", page_size=2)


def test_streamed_export_freezes_frontier_and_shares_identical_bodies(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "同一正文。", request_id="a")
    state = add_node(store, state, "b", "同一正文。", request_id="b")
    stream = store.iter_export("g", page_size=2)
    manifest = next(stream)
    assert manifest["head"]["graph_commit"] == 2
    add_node(store, state, "a", "导出开始后新写的正文。", request_id="later")
    rows = list(stream)
    bodies = [row for row in rows if row["record_type"] == "body"]
    assert len(bodies) == 1 and bodies[0]["text"] == "同一正文。"
    assert rows[-1]["complete"] and rows[-1]["object_versions"] == manifest["object_versions"]
    assert rows[-1]["events"] == manifest["events"]


def test_reader_publishes_world_growth_without_auxiliary_window_or_cursor_advance(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "门前。", request_id="seed")
    entry = state.windows["a"].working_node_id
    state = navigate(store, state, "r1", entry, observed_graph_commit=1, request_id="visit")
    visit = store.list("g", "visit", scope_id="reading:r1")[0]
    method = store.put("g", "method", MethodVersion(id="reader-method", scope_id="reading:r1",
                                                   private_scope="reading:r1", instruction="展开门前的另一种理解。"))
    refs = [ObjectRef(kind="visit", object_id=visit["id"], version=visit["version"]),
            ObjectRef(kind="method", object_id=method["id"], version=method["version"])]
    before = state.reading_tracks["r1"].model_copy(deep=True)
    published = add_node(store, state, "r1", "门前的人听见门内另一种回答。", parents=[entry],
                         input_refs=refs, author_scope="reading:r1", relation="将阅读中的理解变成新枝桠",
                         request_id="reader-growth")
    reader = published.reading_tracks["r1"]
    new_id = reader.bookmark_node_ids[-1]
    assert published.graph_commit == 2 and len(published.windows) == 2
    assert reader.cursor_node_id == before.cursor_node_id and reader.read_seq == before.read_seq == 1
    assert reader.revision == before.revision + 1 and reader.observed_graph_commit == 1
    node = store.get("g", "node", new_id)
    assert node["scope_id"] == "reading:r1" and node["private_scope"] is None
    assert store.list("g", "link")[0]["relation"] == "将阅读中的理解变成新枝桠"
    other = navigate(store, published, "r2", new_id, observed_graph_commit=2, request_id="other-read")
    assert other.reading_tracks["r2"].cursor_node_id == new_id
    assert other.reading_tracks["r1"].cursor_node_id == entry
    assert replay_operations(store, "g", page_size=2)["operations_replayed"]["growth"] == 2


def test_reader_publication_role_permission_private_taint_and_idempotency(graph):
    store, initial = graph
    with pytest.raises(GrowthError, match="工作位置"):
        add_node(store, initial, "r1", "不能默认为一个窗口。", request_id="bad")
    with pytest.raises(GrowthError, match="不一致"):
        add_node(store, initial, "r1", "另一读轨不能冒名发布。", author_scope="reading:r2", request_id="bad")
    with pytest.raises(GrowthError, match="明确指定"):
        add_node(store, initial, "r1", "未知角色。", author_scope="operator:r1", request_id="bad")
    private = add_node(store, initial, "r1", "私下保留的理解。", private_scope="reading:r1",
                       author_scope="reading:r1", request_id="private")
    private_id = private.reading_tracks["r1"].bookmark_node_ids[-1]
    next_state = add_node(store, private, "r1", "继续这个私下理解。", parents=[private_id],
                         author_scope="reading:r1", request_id="continued")
    new_id = next_state.reading_tracks["r1"].bookmark_node_ids[-1]
    assert store.get("g", "node", new_id) is None
    assert store.get("g", "node", new_id, scope_id="reading:r1")["private_scope"] == "reading:r1"
    repeated = add_node(store, next_state, "r1", "继续这个私下理解。", parents=[private_id],
                        author_scope="reading:r1", request_id="continued")
    assert repeated.graph_commit == 2 and repeated.reading_tracks["r1"].read_seq == 0
    with pytest.raises(GrowthError, match="不可见"):
        add_node(store, repeated, "r2", "另一读轨不能使用它。", parents=[private_id],
                 author_scope="reading:r2", request_id="steal")


def test_actual_private_source_span_taints_public_parent_publication_without_inventing_edge(graph):
    store, initial = graph
    state = add_node(store, initial, "a", "甲😀乙的私下记忆。", private_scope="window:a", request_id="private")
    private_id = state.windows["a"].working_node_id
    private = store.get("g", "node", private_id, scope_id="window:a")
    state = add_node(store, state, "b", "门前的公开叙事。", request_id="public")
    public_id = state.windows["b"].working_node_id
    actual_range = SourceSpan(node_id=private_id, node_version=private["version"],
                              text_ref=private["text_ref"], start=1, end=2)
    refs = [ObjectRef(kind="node", object_id=private_id, version=private["version"])]
    published = add_node(store, state, "a", "门前响起了另一声回响。", parents=[public_id],
                         input_refs=refs, source_spans=[actual_range.model_dump()], request_id="publication")
    child_id = published.windows["a"].working_node_id
    assert store.get("g", "node", child_id) is None
    assert store.get("g", "node", child_id, scope_id="window:b") is None
    child = store.get("g", "node", child_id, scope_id="window:a")
    assert child["private_scope"] == "window:a" and child["source_spans"] == [actual_range.model_dump()]
    assert store.get_body(child["source_spans"][0]["text_ref"], 1, 2) == "😀"
    links = store.list("g", "link", scope_id="window:a")
    assert [(link["source_id"], link["target_id"]) for link in links] == [(public_id, child_id)]
    assert store.list("g", "link") == []
    assert replay_operations(store, "g", page_size=2)["operations_replayed"]["growth"] == 3
    with pytest.raises(GrowthError, match="幂等"):
        add_node(store, published, "a", "门前响起了另一声回响。", parents=[public_id],
                 input_refs=refs, source_spans=[], request_id="publication")
    wrong_version = actual_range.model_copy(update={"node_version": "another-version"})
    before = store.load("g").model_dump()
    with pytest.raises(GrowthError, match="精确节点版本"):
        add_node(store, published, "a", "无法使用错误版本。", source_spans=[wrong_version])
    assert store.load("g").model_dump() == before
