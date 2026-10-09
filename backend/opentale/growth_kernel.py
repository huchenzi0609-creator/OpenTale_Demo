"""Deterministic growth transactions and independent reading traversal.

Ordinary input references are dependencies only. A narrative edge is installed
only when the patch explicitly declares it. This module never calls a provider.
"""

from __future__ import annotations

import hashlib
import uuid

from .growth_store import GrowthStore, digest
from .growth_types import (
    GraphState,
    GrowthCommit,
    GrowthPatch,
    LinkDraft,
    NarrativeLink,
    NarrativeNode,
    NodeDraft,
    ObjectRef,
    ReadingPassage,
    ReadVisit,
    SourceSpan,
    canonical_scope,
    utc_now,
)


class GrowthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, False


KernelError = GrowthError


def object_id(run_id: str, action_id: str, suffix: str) -> str:
    return f"{suffix.split(':', 1)[0]}-{hashlib.sha256(f'{run_id}:{action_id}:{suffix}'.encode()).hexdigest()[:24]}"


def _head(store: GrowthStore, state: GraphState) -> GraphState:
    current = store.load_head(state.run_id)
    if current is None:
        raise GrowthError("RUN_NOT_FOUND", "工作记录不存在。")
    return current.model_copy(deep=True)


def _node(store: GrowthStore, run_id: str, node_id: str, scope: str, commit: int) -> dict:
    value = store.get(run_id, "node", node_id, at_commit=commit, scope_id=scope)
    if value is None:
        raise GrowthError("SOURCE_UNAVAILABLE", "此来源在当前作用域与图版本中不可见。")
    return value


def _check_spans(store: GrowthStore, state: GraphState, scope: str, spans: list[SourceSpan], commit: int) -> None:
    for span in spans:
        node = _node(store, state.run_id, span.node_id, scope, commit)
        if node["version"] != span.node_version or node["text_ref"] != span.text_ref.model_dump():
            raise GrowthError("SOURCE_VERSION_MISMATCH", "来源区间不匹配精确节点版本。")
        store.read_text(span.text_ref, span.start, span.end)


def _once(store: GrowthStore, state: GraphState, kind: str, action_id: str, payload: dict) -> dict | None:
    old = store.get(state.run_id, kind, action_id, include_private=True)
    if old is not None and old["payload_hash"] != digest(payload):
        raise GrowthError("IDEMPOTENCY_CONFLICT", "已执行动作的幂等标识不能用于不同内容。")
    return old


def _patch_payload(patch: GrowthPatch) -> dict:
    value = patch.model_dump(mode="json")
    # The optional author field is additive: pre-reader-publication patches retain
    # their original window-only hash and idempotency identity.
    if value["author_scope"] is None:
        value.pop("author_scope")
    return value


def commit_growth(store: GrowthStore, state: GraphState, patch: GrowthPatch) -> GrowthCommit:
    """Publish a multi-node patch atomically; a stale global base remains valid.

    Only an explicit window revision creates a local optimistic-lock condition.
    Earlier drafts in a patch may parent later drafts. Association edges can cycle.
    """
    payload = _patch_payload(patch)
    with store.transaction():
        current = _head(store, state)
        old = _once(store, current, "patch", patch.id, payload)
        if old:
            return GrowthCommit(state=current, local_ids=old["local_ids"], node_ids=old["node_ids"],
                                link_ids=old["link_ids"], idempotent=True)
        scope = patch.author_scope or f"window:{patch.window_id.removeprefix('window:')}"
        if not scope.startswith(("window:", "reading:")):
            raise GrowthError("AUTHOR_SCOPE", "发布作用域必须明确指定生成窗口或读轨。")
        role, owner_id = scope.split(":", 1)
        submitted_owner = patch.window_id.removeprefix(f"{role}:")
        if owner_id != submitted_owner:
            raise GrowthError("AUTHOR_SCOPE_MISMATCH", "发布者标识与其作用域不一致。")
        owner = current.windows.get(owner_id) if role == "window" else current.reading_tracks.get(owner_id)
        if owner is None:
            raise GrowthError("SCOPE_NOT_FOUND", "发布的工作位置不存在。")
        if patch.base_graph_commit > current.graph_commit:
            raise GrowthError("FUTURE_GRAPH", "基础图版本尚未存在。")
        if patch.expected_window_revision is not None and owner.revision != patch.expected_window_revision:
            raise GrowthError("SCOPE_CONFLICT", "发布位置已被另一项工作改变。")
        ids = [draft.local_id for draft in patch.nodes]
        if len(set(ids)) != len(ids) or not all(ids):
            raise GrowthError("DUPLICATE_LOCAL_ID", "补丁中的局部节点标识必须唯一。")
        local_ids = {local: object_id(current.run_id, patch.id, f"node:{local}") for local in ids}
        order = {local: index for index, local in enumerate(ids)}
        scope = canonical_scope(current, scope)
        commit = current.graph_commit + 1
        for ref in patch.input_refs:
            value = store.get(current.run_id, ref.kind, ref.object_id, version=ref.version,
                              at_commit=patch.base_graph_commit, scope_id=scope)
            if value is None:
                raise GrowthError("INPUT_UNAVAILABLE", "输入的精确版本不可见。")
        for draft in patch.nodes:
            if draft.private_scope is not None and canonical_scope(current, draft.private_scope) != scope:
                raise GrowthError("SCOPE_PERMISSION", "不能发布其他作用域的私有节点。")
            _check_spans(store, current, scope, draft.source_spans, patch.base_graph_commit)
        endpoint_nodes: dict[str, dict] = {}
        for link in patch.links:
            for endpoint in (link.source_id, link.target_id):
                if endpoint not in local_ids:
                    endpoint_nodes[endpoint] = _node(
                        store, current.run_id, endpoint, scope, patch.base_graph_commit,
                    )
            if link.kind == "growth":
                if link.target_id not in local_ids:
                    raise GrowthError("GROWTH_TARGET", "生长边必须指向本次新构造。")
                if link.source_id in order and order[link.source_id] >= order[link.target_id]:
                    raise GrowthError("GROWTH_ORDER", "同次生长只能承接较早构造。")
            _check_spans(store, current, scope, link.source_spans, patch.base_graph_commit)
        working = patch.working_node_id or (ids[-1] if ids else
                                           (owner.working_node_id if role == "window" else owner.cursor_node_id))
        if working and working not in local_ids:
            _node(store, current.run_id, working, scope, patch.base_graph_commit)
        nodes: list[dict] = []
        for draft in patch.nodes:
            private_scope = draft.private_scope
            if any(_node(store, current.run_id, span.node_id, scope, patch.base_graph_commit).get("private_scope")
                   for span in draft.source_spans):
                private_scope = scope
            # A declared continuation of private narrative cannot become public
            # merely by omitting a source-span field. Method/feedback dependencies
            # remain diagnostic and do not taint otherwise-public narrative.
            if any(endpoint_nodes[link.source_id].get("private_scope")
                   for link in patch.links if link.kind == "growth" and link.target_id == draft.local_id):
                private_scope = scope
            value = NarrativeNode(id=local_ids[draft.local_id], text_ref=store.put_text(draft.text),
                                  created_commit=commit, scope_id=scope, source_spans=draft.source_spans,
                                  private_scope=private_scope)
            node = store.put(current.run_id, "node", value, commit_seq=commit)
            nodes.append(node)
            endpoint_nodes[draft.local_id] = node
        links: list[dict] = []
        for index, draft in enumerate(patch.links):
            private_scope = scope if any(
                endpoint_nodes[endpoint].get("private_scope") for endpoint in (draft.source_id, draft.target_id)
            ) else None
            if any(_node(store, current.run_id, span.node_id, scope, patch.base_graph_commit).get("private_scope")
                   for span in draft.source_spans):
                private_scope = scope
            value = NarrativeLink(
                id=object_id(current.run_id, patch.id, f"link:{index}"),
                source_id=local_ids.get(draft.source_id, draft.source_id),
                target_id=local_ids.get(draft.target_id, draft.target_id), kind=draft.kind,
                relation=draft.relation, scope_id=scope, created_commit=commit,
                source_spans=draft.source_spans, private_scope=private_scope,
            )
            links.append(store.put(current.run_id, "link", value, commit_seq=commit))
        if role == "window":
            owner.working_node_id = local_ids.get(working, working)
            if not owner.entry_node_ids and nodes:
                owner.entry_node_ids = [nodes[0]["id"]]
            # The frontier is a bounded working set, not every created node.
            parents = {link["source_id"] for link in links if link["kind"] == "growth"}
            owner.frontier_node_ids = list(dict.fromkeys(
                [node for node in owner.frontier_node_ids if node not in parents] + [node["id"] for node in nodes]
            ))[-64:]
        else:
            # Publishing is world construction, not a visit or rendition.
            owner.bookmark_node_ids = list(dict.fromkeys(
                owner.bookmark_node_ids + [node["id"] for node in nodes]
            ))[-64:]
        owner.revision += 1
        current.graph_commit = commit
        current.updated_at = utc_now()
        current.counts["nodes"] = store.count_objects(current.run_id, "node")
        current.counts["links"] = store.count_objects(current.run_id, "link")
        # Dependency evidence is private and distinct from declared narrative links.
        store.put(current.run_id, "dependency", {"id": patch.id, "scope_id": scope,
                  "private_scope": scope, "input_refs": [ref.model_dump() for ref in patch.input_refs],
                  "base_graph_commit": patch.base_graph_commit}, commit_seq=commit)
        patch_record = {"id": patch.id, "payload_hash": digest(payload), "local_ids": local_ids,
                        "node_ids": [node["id"] for node in nodes], "link_ids": [link["id"] for link in links],
                        "scope_id": scope, "created_commit": commit,
                        "operation_hash": digest({key: value for key, value in payload.items()
                                                  if key not in {"base_graph_commit", "expected_window_revision"}})}
        store.put(current.run_id, "patch", patch_record, commit_seq=commit)
        store.save_head(current)
        event_patch = {**payload, "nodes": [
            {**{key: value for key, value in draft.items() if key != "text"}, "text_ref": node["text_ref"]}
            for draft, node in zip(payload["nodes"], nodes, strict=True)
        ]}
        store.record_event(current.run_id, "growth", {"patch": event_patch, "patch_format": "body-refs-v1",
                                                      "result": patch_record, "graph_commit": commit})
        return GrowthCommit(state=current, local_ids=local_ids, node_ids=patch_record["node_ids"],
                            link_ids=patch_record["link_ids"])


def add_node(
    store: GrowthStore, state: GraphState, window_id: str, text: str, *,
    parents: list[str] | None = None, associations: list[str] | None = None,
    input_refs: list[ObjectRef | dict] | None = None, request_id: str | None = None,
    private_scope: str | None = None, expected_revision: int | None = None,
    author_scope: str | None = None, relation: str = "继续生长",
    source_spans: list[SourceSpan | dict] | None = None,
) -> GraphState:
    action_id = request_id or uuid.uuid4().hex
    patch = GrowthPatch(
        id=action_id, window_id=window_id, author_scope=author_scope, base_graph_commit=state.graph_commit,
        expected_window_revision=expected_revision,
        nodes=[NodeDraft(local_id="new", text=text, private_scope=private_scope,
                         source_spans=[SourceSpan.model_validate(span) for span in (source_spans or [])])],
        links=[LinkDraft(source_id=parent, target_id="new", kind="growth", relation=relation)
               for parent in (parents or [])] +
              [LinkDraft(source_id="new", target_id=other, kind="association", relation="关联")
               for other in (associations or [])],
        input_refs=[ObjectRef.model_validate(ref) for ref in (input_refs or [])],
    )
    # Accepted-result recovery can retry using an advanced head. A previously
    # committed operation retains its original horizon and local-lock outcome.
    with store.transaction():
        old = store.get(state.run_id, "patch", action_id, include_private=True)
        if old is not None:
            operation = {key: value for key, value in _patch_payload(patch).items()
                         if key not in {"base_graph_commit", "expected_window_revision"}}
            if old.get("operation_hash") != digest(operation):
                raise GrowthError("IDEMPOTENCY_CONFLICT", "已执行动作的幂等标识不能用于不同内容。")
            return _head(store, state)
        return commit_growth(store, state, patch).state


def add_link(
    store: GrowthStore, state: GraphState, source_id: str, target_id: str, *,
    kind: str = "association", relation: str = "", scope_id: str,
    request_id: str | None = None, source_spans: list[SourceSpan | dict] | None = None,
) -> GraphState:
    action_id = request_id or uuid.uuid4().hex
    scope_id = canonical_scope(state, scope_id)
    spans = [SourceSpan.model_validate(span) for span in (source_spans or [])]
    payload = {"source_id": source_id, "target_id": target_id, "kind": kind, "relation": relation,
               "scope_id": scope_id, "source_spans": [span.model_dump() for span in spans]}
    with store.transaction():
        current = _head(store, state)
        if _once(store, current, "link_action", action_id, payload):
            return current
        scope_id = canonical_scope(current, scope_id)
        if (scope_id.removeprefix("window:") not in current.windows if scope_id.startswith("window:") else
                scope_id.removeprefix("reading:") not in current.reading_tracks):
            raise GrowthError("SCOPE_NOT_FOUND", "关系声明的工作位置不存在。")
        source = _node(store, current.run_id, source_id, scope_id, state.graph_commit)
        target = _node(store, current.run_id, target_id, scope_id, state.graph_commit)
        if kind not in {"growth", "association"}:
            raise GrowthError("LINK_KIND", "关系类别无效。")
        if kind == "growth" and source["created_commit"] >= target["created_commit"]:
            raise GrowthError("GROWTH_ORDER", "生长关系必须承接较早构造。")
        _check_spans(store, current, scope_id, spans, state.graph_commit)
        commit = current.graph_commit + 1
        private_scope = scope_id if source.get("private_scope") or target.get("private_scope") else None
        if any(_node(store, current.run_id, span.node_id, scope_id, state.graph_commit).get("private_scope")
               for span in spans):
            private_scope = scope_id
        link = store.put(current.run_id, "link", NarrativeLink(
            id=object_id(current.run_id, action_id, "link:0"), source_id=source_id,
            target_id=target_id, kind=kind, relation=relation, scope_id=scope_id,
            created_commit=commit, source_spans=spans, private_scope=private_scope,
        ), commit_seq=commit)
        store.put(current.run_id, "link_action", {"id": action_id, "payload_hash": digest(payload),
                  "link_id": link["id"]}, commit_seq=commit)
        current.graph_commit = commit
        current.counts["links"] = store.count_objects(current.run_id, "link")
        current.updated_at = utc_now()
        store.save(current)
        store.record_event(current.run_id, "association", {"action_id": action_id, "link": link})
        return current


def navigate(
    store: GrowthStore, state: GraphState, track_id: str, node_id: str, *,
    via_link_id: str | None = None, transition: str = "", start: int = 0, end: int | None = None,
    observed_graph_commit: int | None = None, request_id: str | None = None,
    expected_revision: int | None = None,
) -> GraphState:
    """Visit/revisit a frozen node without advancing the world's graph commit."""
    action_id = request_id or uuid.uuid4().hex
    payload = {"track_id": track_id, "node_id": node_id, "via_link_id": via_link_id,
               "transition": transition, "start": start, "end": end,
               "observed_graph_commit": observed_graph_commit, "expected_revision": expected_revision}
    with store.transaction():
        current = _head(store, state)
        if _once(store, current, "read_action", action_id, payload):
            return current
        track_key = track_id.removeprefix("reading:")
        track = current.reading_tracks.get(track_key)
        if track is None:
            raise GrowthError("SCOPE_NOT_FOUND", "读轨不存在。")
        if expected_revision is not None and expected_revision != track.revision:
            raise GrowthError("SCOPE_CONFLICT", "读轨游标已改变。")
        observed = track.observed_graph_commit if observed_graph_commit is None else observed_graph_commit
        if observed > current.graph_commit or observed < 0:
            raise GrowthError("FUTURE_GRAPH", "读取图版本不存在。")
        scope = canonical_scope(current, f"reading:{track_key}")
        node = _node(store, current.run_id, node_id, scope, observed)
        if via_link_id is not None:
            link = store.get(current.run_id, "link", via_link_id, at_commit=observed, scope_id=scope)
            if link is None or track.cursor_node_id is None or {
                link["source_id"], link["target_id"]
            } != {track.cursor_node_id, node_id}:
                raise GrowthError("INVALID_TRAVERSAL", "此关系不能连接当前游标与目标节点。")
        elif track.cursor_node_id is None:
            if track.entry_node_ids and node_id not in track.entry_node_ids and not transition:
                raise GrowthError("ENTRY_TRANSITION_REQUIRED", "跨入口访问需要明确的读者过渡。")
        elif node_id != track.cursor_node_id and not transition:
            # A revisit is an explicit transition too; it never forges a world edge.
            raise GrowthError("READER_TRANSITION_REQUIRED", "跨节点访问需要关系或读者过渡。")
        stop = node["text_ref"]["length"] if end is None else end
        try:
            span = SourceSpan(node_id=node_id, node_version=node["version"],
                              text_ref=node["text_ref"], start=start, end=stop)
            store.read_text(span.text_ref, start, stop)
        except (ValueError, KeyError):
            raise GrowthError("SOURCE_RANGE", "读取区间无效。") from None
        visit = store.put(current.run_id, "visit", ReadVisit(
            id=object_id(current.run_id, action_id, "visit:0"), track_id=track_key,
            read_seq=track.read_seq + 1, node_id=node_id, node_version=node["version"],
            observed_graph_commit=observed, from_node_id=track.cursor_node_id,
            via_link_id=via_link_id, transition=transition, source_spans=[span],
            private_scope=scope,
        ))
        store.put(current.run_id, "read_action", {"id": action_id, "payload_hash": digest(payload),
                  "visit_id": visit["id"], "private_scope": scope})
        track.read_seq += 1
        track.revision += 1
        track.observed_graph_commit = observed
        track.cursor_node_id = node_id
        current.counts["visits"] = store.count_objects(current.run_id, "visit")
        current.updated_at = utc_now()
        store.save(current)
        store.record_event(current.run_id, "visit", {"action_id": action_id, "visit": visit})
        return current


def add_passage(
    store: GrowthStore, state: GraphState, track_id: str, text: str, *, visit_ids: list[str],
    source_spans: list[SourceSpan | dict] | None = None, request_id: str | None = None,
) -> GraphState:
    """Save a rendition only from actual visits, with exact source ranges."""
    action_id = request_id or uuid.uuid4().hex
    spans = [SourceSpan.model_validate(span) for span in (source_spans or [])]
    payload = {"track_id": track_id, "text": text, "visit_ids": visit_ids,
               "source_spans": [span.model_dump() for span in spans]}
    with store.transaction():
        current = _head(store, state)
        if _once(store, current, "passage_action", action_id, payload):
            return current
        track_key = track_id.removeprefix("reading:")
        scope = canonical_scope(current, f"reading:{track_key}")
        track = current.reading_tracks.get(track_key)
        if track is None or not visit_ids:
            raise GrowthError("VISITS_REQUIRED", "呈现需要此读轨的实际访问。")
        visits = []
        for visit_id in visit_ids:
            visit = store.get(current.run_id, "visit", visit_id, scope_id=scope)
            if visit is None or visit["track_id"] != track_key:
                raise GrowthError("VISIT_UNAVAILABLE", "呈现不能使用其他读轨或不存在的访问。")
            visits.append(visit)
        available = [SourceSpan.model_validate(span) for visit in visits for span in visit["source_spans"]]
        if not spans:
            spans = available
        for span in spans:
            if not any(old.node_id == span.node_id and old.node_version == span.node_version
                       and old.text_ref == span.text_ref and old.start <= span.start <= span.end <= old.end
                       for old in available):
                raise GrowthError("UNVISITED_SOURCE", "呈现来源必须落在实际访问过的区间中。")
            if span.passage_end is not None and span.passage_end > len(text):
                raise GrowthError("PASSAGE_RANGE", "呈现映射区间超出正文。")
        passage = store.put(current.run_id, "passage", ReadingPassage(
            id=object_id(current.run_id, action_id, "passage:0"), track_id=track_key,
            read_seq=track.read_seq + 1, text_ref=store.put_text(text),
            visit_ids=visit_ids, source_spans=spans,
            private_scope=scope,
        ))
        store.put(current.run_id, "passage_action", {"id": action_id, "payload_hash": digest(payload),
                  "passage_id": passage["id"], "private_scope": scope})
        track.read_seq += 1
        track.revision += 1
        current.counts["passages"] = store.count_objects(current.run_id, "passage")
        current.updated_at = utc_now()
        store.save(current)
        store.record_event(current.run_id, "passage", {"action_id": action_id, "passage": passage})
        return current
