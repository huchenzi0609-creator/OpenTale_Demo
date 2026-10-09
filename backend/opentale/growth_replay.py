"""Bounded evidence verification; integrity is explicitly separate from semantic replay."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .growth_kernel import GrowthError, add_link, add_passage, commit_growth, navigate
from .growth_methods import adopt_method
from .growth_store import GrowthStore, digest
from .growth_types import GraphState, GrowthPatch, ObjectRef, SourceSpan


def _restore_patch(store: GrowthStore, value: dict[str, Any]) -> dict[str, Any]:
    """Resolve one patch's CAS bodies; earlier inline-text events stay readable."""
    nodes = []
    for original in value["nodes"]:
        draft = dict(original)
        if "text_ref" in draft:
            ref = draft.pop("text_ref")
            text = store.read_text(ref)
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != ref["sha256"]:
                raise GrowthError("BODY_INTEGRITY", "生长事件的正文内容寻址校验失败。")
            draft["text"] = text
        nodes.append(draft)
    return {**value, "nodes": nodes}


def verify_objects(
    store: GrowthStore, run_id: str, *, after_seq: int = 0, limit: int = 100,
) -> dict[str, Any]:
    """Verify one indexed page of every immutable version, reading bodies on demand."""
    rows = store.list_versions(run_id, after_seq=after_seq, limit=limit)
    for row in rows:
        value = dict(row["value"])
        version = value.pop("version")
        if digest(value) != version:
            raise GrowthError("OBJECT_INTEGRITY", "不可变对象校验失败。")
        text_ref = value.get("text_ref")
        if text_ref:
            text = store.read_text(text_ref)
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_ref["sha256"]:
                raise GrowthError("BODY_INTEGRITY", "正文内容寻址校验失败。")
        for field in ("source_spans", "visible_spans"):
            for raw in value.get(field, []):
                span = SourceSpan.model_validate(raw)
                node = store.get(run_id, "node", span.node_id, version=span.node_version, include_private=True)
                if node is None or node["text_ref"] != span.text_ref.model_dump():
                    raise GrowthError("SOURCE_INTEGRITY", "来源区间的精确版本不存在。")
                store.read_text(span.text_ref, span.start, span.end)
        if row["kind"] == "link":
            for endpoint in (value["source_id"], value["target_id"]):
                if store.get(run_id, "node", endpoint, at_commit=row["commit_seq"], include_private=True) is None:
                    raise GrowthError("LINK_INTEGRITY", "叙事关系引用不存在的节点。")
    next_seq = rows[-1]["seq"] if rows else after_seq
    more = bool(store.list_versions(run_id, after_seq=next_seq, limit=1))
    return {"level": "object_integrity", "run_id": run_id, "after_seq": after_seq,
            "through_seq": next_seq, "objects_checked": len(rows), "has_more": more,
            "full_object_history": after_seq == 0 and not more, "semantic_replay": False}


def replay_range(
    store: GrowthStore, run_id: str, *, after_seq: int = 0, limit: int = 100,
    checkpoint_ref: ObjectRef | dict | None = None,
) -> dict[str, Any]:
    """Check a hash-linked event range against its base, without loading old snapshots.

    A checkpoint only certifies the verified event prefix, never literary meaning
    or full reconstructed runtime behavior. Range receipts keep that distinction.
    """
    state = store.load(run_id)
    if state is None:
        raise GrowthError("RUN_NOT_FOUND", "工作记录不存在。")
    if after_seq < 0:
        raise GrowthError("REPLAY_RANGE", "重放起始序号不能为负。")
    previous = store.previous_event(run_id, after_seq)
    expected_hash = previous["event_hash"] if previous else ""
    base_verified = after_seq == 0
    checkpoint_identity = None
    if checkpoint_ref is not None:
        ref = ObjectRef.model_validate(checkpoint_ref)
        checkpoint = store.get(run_id, "checkpoint", ref.object_id, version=ref.version, include_private=True)
        if ref.kind != "checkpoint" or checkpoint is None:
            raise GrowthError("CHECKPOINT_UNAVAILABLE", "已验证检查点不存在。")
        if checkpoint["through_seq"] != after_seq or checkpoint["event_hash"] != expected_hash:
            raise GrowthError("CHECKPOINT_MISMATCH", "重放区间与检查点不匹配。")
        base_verified = bool(checkpoint["verified_prefix"])
        checkpoint_identity = ref.model_dump()
    events = store.list_events(run_id, after_seq=after_seq, limit=limit)
    last_commit = None
    for event in events:
        if event["previous_hash"] != expected_hash or event["event_hash"] != digest({
            "kind": event["kind"], "payload": event["payload"], "previous_hash": expected_hash,
        }):
            raise GrowthError("EVENT_INTEGRITY", "执行事件链校验失败。")
        expected_hash = event["event_hash"]
        payload = event["payload"]
        if event["kind"] == "growth":
            result = payload["result"]
            patch = store.get(run_id, "patch", result["id"], include_private=True)
            if patch is None or patch["payload_hash"] != digest(_restore_patch(store, payload["patch"])):
                raise GrowthError("PATCH_INTEGRITY", "生长事务与执行事件不匹配。")
            if last_commit is not None and payload["graph_commit"] <= last_commit:
                raise GrowthError("COMMIT_ORDER", "图提交序号未推进。")
            last_commit = payload["graph_commit"]
            for node_id in result["node_ids"]:
                if store.get(run_id, "node", node_id, at_commit=last_commit, include_private=True) is None:
                    raise GrowthError("PATCH_INTEGRITY", "生长事务缺失节点。")
        elif event["kind"] in {"visit", "passage"}:
            obj = payload[event["kind"]]
            actual = store.get(run_id, event["kind"], obj["id"], version=obj["version"], include_private=True)
            if actual != obj:
                raise GrowthError("READING_INTEGRITY", "阅读记录与执行事件不匹配。")
    through = events[-1]["seq"] if events else after_seq
    more = bool(store.list_events(run_id, after_seq=through, limit=1))
    return {"level": "event_range_integrity", "run_id": run_id, "after_seq": after_seq,
            "through_seq": through, "events_checked": len(events), "event_hash": expected_hash,
            "base_verified": base_verified, "checkpoint_ref": checkpoint_identity, "has_more": more,
            "full_event_history": after_seq == 0 and not more, "semantic_replay": False}


def save_checkpoint(store: GrowthStore, run_id: str, receipt: dict[str, Any]) -> ObjectRef:
    if receipt.get("level") != "event_range_integrity" or receipt.get("run_id") != run_id:
        raise GrowthError("CHECKPOINT_RECEIPT", "检查点需要对应运行的区间校验收据。")
    verified = replay_range(store, run_id, after_seq=receipt["after_seq"],
                            limit=max(1, receipt["events_checked"]),
                            checkpoint_ref=receipt.get("checkpoint_ref"))
    if any(verified[key] != receipt[key] for key in ("through_seq", "event_hash", "base_verified")):
        raise GrowthError("CHECKPOINT_RECEIPT", "检查点收据未通过重新校验。")
    previous = store.previous_event(run_id, receipt["through_seq"])
    actual_hash = previous["event_hash"] if previous else ""
    if actual_hash != receipt["event_hash"]:
        raise GrowthError("CHECKPOINT_RECEIPT", "检查点收据与实际事件链不匹配。")
    value = {"id": f"checkpoint-{receipt['through_seq']}", "through_seq": receipt["through_seq"],
             "event_hash": actual_hash, "verified_prefix": receipt["base_verified"],
             "verification_level": "event_range_integrity", "semantic_replay": False}
    saved = store.put(run_id, "checkpoint", value)
    return ObjectRef(kind="checkpoint", object_id=saved["id"], version=saved["version"])


verify_range = replay_range


def _check_head_refs(store: GrowthStore, head: GraphState, seen_bodies: set[str]) -> None:
    """Resolve only the bounded pointers held by one head, never expand its graph."""
    for kind, positions in (("window", head.windows), ("reading", head.reading_tracks)):
        for key, actor in positions.items():
            scope = f"{kind}:{key}"
            horizon = head.graph_commit if kind == "window" else actor.observed_graph_commit
            if horizon > head.graph_commit:
                raise GrowthError("HEAD_GRAPH_HORIZON", "读轨的图视界超过当前图。")
            ids = list(actor.entry_node_ids)
            if kind == "window":
                ids += actor.frontier_node_ids + ([actor.working_node_id] if actor.working_node_id else [])
                seed = actor.seed_text_ref
                if seed and seed.sha256 not in seen_bodies:
                    text = store.read_text(seed)
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() != seed.sha256:
                        raise GrowthError("BODY_INTEGRITY", "私有种子正文校验失败。")
                    seen_bodies.add(seed.sha256)
            else:
                ids += actor.bookmark_node_ids + ([actor.cursor_node_id] if actor.cursor_node_id else [])
            # Entrances/bookmarks may be known before a track opts into the new
            # graph horizon; its current cursor must belong to the frozen horizon.
            for node_id in set(ids):
                selected_horizon = horizon if kind == "reading" and node_id == actor.cursor_node_id else head.graph_commit
                if store.get(head.run_id, "node", node_id, at_commit=selected_horizon, scope_id=scope) is None:
                    raise GrowthError("HEAD_REFERENCE", "状态头指向不可用的叙事节点。")
            if actor.method_ref:
                ref = actor.method_ref
                if ref.kind != "method" or store.get(head.run_id, "method", ref.object_id,
                                                      version=ref.version, scope_id=scope) is None:
                    raise GrowthError("HEAD_METHOD_REFERENCE", "状态头采用的方法精确版本不可用。")


def verify_head_replay(
    store: GrowthStore, run_id: str, *, after_seq: int = 0, limit: int = 100,
    checkpoint_ref: ObjectRef | dict | None = None,
) -> dict[str, Any]:
    """Reconstruct a head transition range, resuming from a verified small checkpoint.

    This checks actual stored-state transitions and linked immutable pointers. It
    does not by itself re-execute graph operations; use replay_operations for that.
    """
    if after_seq < 0:
        raise GrowthError("REPLAY_RANGE", "重放起始序号不能为负。")
    with store.transaction():
        current = store.load(run_id)
        if current is None:
            raise GrowthError("RUN_NOT_FOUND", "工作记录不存在。")
        previous = store.previous_event(run_id, after_seq)
        expected_hash = previous["event_hash"] if previous else ""
        head = None
        head_hash = None
        base_verified = after_seq == 0
        checkpoint_identity = None
        if checkpoint_ref is not None:
            ref = ObjectRef.model_validate(checkpoint_ref)
            checkpoint = store.get(run_id, "head_checkpoint", ref.object_id,
                                   version=ref.version, include_private=True)
            if ref.kind != "head_checkpoint" or checkpoint is None or checkpoint.get("run_id") != run_id:
                raise GrowthError("CHECKPOINT_UNAVAILABLE", "状态重放检查点不存在。")
            if (checkpoint["through_seq"] != after_seq or checkpoint["event_hash"] != expected_hash
                    or not checkpoint["verified_prefix"]):
                raise GrowthError("CHECKPOINT_MISMATCH", "状态重放区间与已验证检查点不匹配。")
            head = GraphState.model_validate(checkpoint["head"])
            head_hash = digest(head.model_dump(mode="json"))
            if head_hash != checkpoint["head_hash"]:
                raise GrowthError("CHECKPOINT_INTEGRITY", "状态检查点正文校验失败。")
            base_verified = True
            checkpoint_identity = ref.model_dump()
        elif after_seq:
            raise GrowthError("HEAD_CHECKPOINT_REQUIRED", "状态重放续读需要已验证的状态检查点。")
        events = store.list_events(run_id, after_seq=after_seq, limit=limit)
        seen_bodies: set[str] = set()
        transitions = 0
        for event in events:
            if event["previous_hash"] != expected_hash or event["event_hash"] != digest({
                "kind": event["kind"], "payload": event["payload"], "previous_hash": expected_hash,
            }):
                raise GrowthError("EVENT_INTEGRITY", "执行事件链校验失败。")
            expected_hash = event["event_hash"]
            payload = event["payload"]
            if event["kind"] == "head_initial":
                if head is not None:
                    raise GrowthError("HEAD_INITIAL_ORDER", "初始状态头重复出现。")
            elif event["kind"] == "head_transition":
                if head is None or payload["before_hash"] != head_hash:
                    raise GrowthError("HEAD_TRANSITION_BASE", "状态转换没有匹配其前一状态。")
            else:
                continue
            next_head = GraphState.model_validate(payload["head"])
            next_hash = digest(next_head.model_dump(mode="json"))
            if next_head.run_id != run_id or next_hash != payload["head_hash"]:
                raise GrowthError("HEAD_TRANSITION_INTEGRITY", "状态转换内容校验失败。")
            if head and next_head.graph_commit < head.graph_commit:
                raise GrowthError("HEAD_COMMIT_ORDER", "状态转换回退了公开图提交。")
            _check_head_refs(store, next_head, seen_bodies)
            head, head_hash = next_head, next_hash
            transitions += 1
        if head is None:
            raise GrowthError("HEAD_HISTORY_UNAVAILABLE", "此存档未保存可重建的初始状态头。")
        through = events[-1]["seq"] if events else after_seq
        more = bool(store.list_events(run_id, after_seq=through, limit=1))
        final_matches = None if more else head.model_dump(mode="json") == current.model_dump(mode="json")
        if final_matches is False:
            raise GrowthError("HEAD_REPLAY_MISMATCH", "重建状态与当前存档头不一致。")
        return {"level": "head_transition_replay", "run_id": run_id, "after_seq": after_seq,
                "through_seq": through, "event_hash": expected_hash, "head_hash": head_hash,
                "head": head.model_dump(mode="json"), "events_checked": len(events),
                "transitions_reconstructed": transitions, "base_verified": base_verified,
                "checkpoint_ref": checkpoint_identity, "has_more": more,
                "current_head_matches": final_matches, "verified_prefix": base_verified,
                "operational_replay": False, "semantic_replay": False}


def save_head_checkpoint(store: GrowthStore, run_id: str, receipt: dict[str, Any]) -> ObjectRef:
    """Persist a revalidated small head checkpoint for the next bounded replay page."""
    if receipt.get("level") != "head_transition_replay" or receipt.get("run_id") != run_id:
        raise GrowthError("CHECKPOINT_RECEIPT", "状态检查点需要对应运行的状态重放收据。")
    verified = verify_head_replay(store, run_id, after_seq=receipt["after_seq"],
                                 limit=max(1, receipt["events_checked"]),
                                 checkpoint_ref=receipt.get("checkpoint_ref"))
    if any(verified[key] != receipt[key] for key in ("through_seq", "event_hash", "head_hash", "head")):
        raise GrowthError("CHECKPOINT_RECEIPT", "状态重放收据未通过重新校验。")
    value = {"id": f"head-checkpoint-{receipt['through_seq']}", "run_id": run_id,
             "through_seq": receipt["through_seq"], "event_hash": receipt["event_hash"],
             "head": receipt["head"], "head_hash": receipt["head_hash"],
             "verified_prefix": receipt["verified_prefix"], "verification_level": "head_transition_replay"}
    saved = store.put(run_id, "head_checkpoint", value)
    return ObjectRef(kind="head_checkpoint", object_id=saved["id"], version=saved["version"])


def replay_operations(store: GrowthStore, run_id: str, *, page_size: int = 100) -> dict[str, Any]:
    """Re-execute deterministic core actions offline in an isolated indexed database.

    Provider outputs and generic work records are frozen boundary inputs. Graph
    patches, association declarations, visits, passages, and core method adoption
    are actually executed by their original kernel and compared to saved results.
    Generic runtime head controls are reconstructed from their hash-linked state
    transitions. A snapshot fork explicitly starts from its captured baseline.
    """
    store._page_bounds(0, page_size)
    with store.transaction(), TemporaryDirectory(prefix="opentale-replay-") as directory:
        replay = GrowthStore(Path(directory))
        try:
            return _replay_operations(store, replay, run_id, page_size)
        finally:
            replay.close()


def _replay_operations(source: GrowthStore, replay: GrowthStore, run_id: str, page_size: int) -> dict[str, Any]:
    actual_head = source.load(run_id)
    if actual_head is None:
        raise GrowthError("RUN_NOT_FOUND", "工作记录不存在。")
    cursor, event_hash = 0, ""
    pending: tuple[GraphState, GraphState] | None = None
    head_seen = False
    operations = {"growth": 0, "association": 0, "visit": 0, "passage": 0, "adopt_method": 0}
    controls, total_events, baseline_commit = 0, 0, 0

    def copy_bodies(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"sha256", "length"}:
                try:
                    replay.read_text(value, 0, 0)
                except KeyError:
                    copied = replay.put_text(source.read_text(value)).model_dump()
                    if copied != value:
                        raise GrowthError("BODY_INTEGRITY", "重放输入正文校验失败。")
            else:
                for member in value.values():
                    copy_bodies(member)
        elif isinstance(value, list):
            for member in value:
                copy_bodies(member)

    def copy_ref(ref: ObjectRef | dict, horizon: int) -> None:
        frozen = ObjectRef.model_validate(ref)
        if replay.get(run_id, frozen.kind, frozen.object_id, version=frozen.version, include_private=True):
            return
        if frozen.kind in {"node", "link", "visit", "passage"}:
            raise GrowthError("REPLAY_SOURCE_ORDER", "核心动作使用了尚未重放构造的来源。")
        value = source.get(run_id, frozen.kind, frozen.object_id, version=frozen.version,
                           at_commit=horizon, include_private=True)
        if value is None:
            raise GrowthError("REPLAY_INPUT_UNAVAILABLE", "冻结的重放输入版本不存在。")
        copy_bodies(value)
        # Put this frozen boundary input before recursively resolving its sources;
        # this also terminates references among non-executable method objects.
        replay.put(run_id, frozen.kind, value, commit_seq=0)
        for member in value.get("source_refs", []):
            copy_ref(member, horizon)
        for field in ("method_ref", "parent_ref", "evaluation_method_ref"):
            if value.get(field):
                copy_ref(value[field], horizon)

    def prepare_head(head: GraphState) -> None:
        copy_bodies(head.model_dump(mode="json"))
        for actor in [*head.windows.values(), *head.reading_tracks.values()]:
            if actor.method_ref:
                copy_ref(actor.method_ref, head.graph_commit)

    def clean_head(head: GraphState) -> dict:
        value = head.model_dump(mode="json")
        value.pop("updated_at", None)  # wall-clock commit metadata is not fictional state
        return value

    def flush_control() -> None:
        nonlocal pending, controls
        if pending is None:
            return
        before, after = pending
        if before.graph_commit != after.graph_commit:
            raise GrowthError("UNREPLAYED_GRAPH_TRANSITION", "公开图推进缺少可执行核心事件。")
        for key, track in before.reading_tracks.items():
            if key in after.reading_tracks and track.read_seq != after.reading_tracks[key].read_seq:
                raise GrowthError("UNREPLAYED_READ_TRANSITION", "阅读推进缺少可执行访问或呈现事件。")
        prepare_head(after)
        _check_head_refs(replay, after, set())
        replay.save(after)
        pending = None
        controls += 1

    def compare_object(kind: str, object_id: str) -> None:
        expected = replay.get(run_id, kind, object_id, include_private=True)
        actual = source.get(run_id, kind, object_id, include_private=True)
        if expected != actual:
            raise GrowthError("OPERATION_REPLAY_MISMATCH", "核心动作的重放结果与保存对象不一致。")
        if actual and actual.get("text_ref"):
            ref = actual["text_ref"]
            original_body = source.read_text(ref)
            if (hashlib.sha256(original_body.encode("utf-8")).hexdigest() != ref["sha256"]
                    or original_body != replay.read_text(expected["text_ref"])):
                raise GrowthError("OPERATION_BODY_MISMATCH", "核心动作的重建正文与保存正文不一致。")

    while True:
        events = source.list_events(run_id, after_seq=cursor, limit=page_size)
        if not events:
            break
        for event in events:
            if event["previous_hash"] != event_hash or event["event_hash"] != digest({
                "kind": event["kind"], "payload": event["payload"], "previous_hash": event_hash,
            }):
                raise GrowthError("EVENT_INTEGRITY", "执行事件链校验失败。")
            event_hash, cursor = event["event_hash"], event["seq"]
            total_events += 1
            kind, payload = event["kind"], event["payload"]
            if kind == "head_initial":
                if head_seen:
                    raise GrowthError("HEAD_INITIAL_ORDER", "初始状态头重复出现。")
                initial = GraphState.model_validate(payload["head"])
                if initial.run_id != run_id or digest(payload["head"]) != payload["head_hash"]:
                    raise GrowthError("HEAD_TRANSITION_INTEGRITY", "初始状态头校验失败。")
                replay.create(initial)
                head_seen = True
                baseline_commit = initial.graph_commit
                if baseline_commit:
                    # Fork anchors legitimately contain already-constructed objects.
                    for baseline_kind in ("node", "link", "visit", "passage", "patch", "dependency"):
                        offset = 0
                        while True:
                            rows = source.list(run_id, baseline_kind, at_commit=baseline_commit,
                                               include_private=True, offset=offset, limit=page_size)
                            for row in rows:
                                if baseline_kind in {"visit", "passage"}:
                                    track = initial.reading_tracks.get(row["track_id"])
                                    if track is None or row["read_seq"] > track.read_seq:
                                        continue
                                copy_bodies(row)
                                replay.put(run_id, baseline_kind, row, commit_seq=row.get("created_commit", 0))
                            offset += len(rows)
                            if len(rows) < page_size:
                                break
                prepare_head(initial)
                _check_head_refs(replay, initial, set())
                continue
            if not head_seen:
                raise GrowthError("HEAD_HISTORY_UNAVAILABLE", "此存档未保存可重建的初始状态头。")
            if kind == "head_transition":
                flush_control()
                before = replay.load(run_id)
                after = GraphState.model_validate(payload["head"])
                if payload["before_hash"] != digest(before.model_dump(mode="json")):
                    raise GrowthError("HEAD_TRANSITION_BASE", "状态转换没有匹配其前一状态。")
                if after.run_id != run_id or payload["head_hash"] != digest(payload["head"]):
                    raise GrowthError("HEAD_TRANSITION_INTEGRITY", "状态转换内容校验失败。")
                pending = (before, after)
                continue
            if kind not in operations:
                flush_control()
                continue
            if pending is None:
                raise GrowthError("OPERATION_HEAD_MISSING", "核心动作未保存对应状态转换。")
            before, after = pending
            prepare_head(before)
            if kind == "growth":
                patch = GrowthPatch.model_validate(_restore_patch(source, payload["patch"]))
                for ref in patch.input_refs:
                    copy_ref(ref, patch.base_graph_commit)
                result = commit_growth(replay, before, patch)
                rebuilt = result.state
                if (result.local_ids != payload["result"]["local_ids"]
                        or result.node_ids != payload["result"]["node_ids"]
                        or result.link_ids != payload["result"]["link_ids"]):
                    raise GrowthError("OPERATION_REPLAY_MISMATCH", "生长补丁局部引用解析结果不一致。")
                for node_id in result.node_ids:
                    compare_object("node", node_id)
                for link_id in result.link_ids:
                    compare_object("link", link_id)
            elif kind == "association":
                link = payload["link"]
                rebuilt = add_link(replay, before, link["source_id"], link["target_id"], kind=link["kind"],
                    relation=link["relation"], scope_id=link["scope_id"], request_id=payload["action_id"],
                    source_spans=link["source_spans"])
                compare_object("link", link["id"])
            elif kind == "visit":
                visit = payload["visit"]
                if len(visit["source_spans"]) != 1:
                    raise GrowthError("REPLAY_VISIT_FORMAT", "当前访问事件需要一个实际读取区间。")
                span = visit["source_spans"][0]
                rebuilt = navigate(replay, before, visit["track_id"], visit["node_id"],
                    via_link_id=visit["via_link_id"], transition=visit["transition"], start=span["start"],
                    end=span["end"], observed_graph_commit=visit["observed_graph_commit"],
                    request_id=payload["action_id"])
                compare_object("visit", visit["id"])
            elif kind == "passage":
                passage = payload["passage"]
                rebuilt = add_passage(replay, before, passage["track_id"], source.read_text(passage["text_ref"]),
                    visit_ids=passage["visit_ids"], source_spans=passage["source_spans"],
                    request_id=payload["action_id"])
                compare_object("passage", passage["id"])
            else:
                copy_ref(payload["method_ref"], before.graph_commit)
                rebuilt = adopt_method(replay, before, payload["scope_id"], payload["method_ref"])
            if clean_head(rebuilt) != clean_head(after):
                raise GrowthError("OPERATION_HEAD_MISMATCH", "核心动作的重建状态与保存转换不一致。")
            # Reuse the saved timestamp after comparing every deterministic field.
            prepare_head(after)
            replay.save(after)
            _check_head_refs(replay, after, set())
            pending = None
            operations[kind] += 1
    if not head_seen:
        raise GrowthError("HEAD_HISTORY_UNAVAILABLE", "此存档未保存可重建的初始状态头。")
    flush_control()
    if replay.load(run_id).model_dump(mode="json") != actual_head.model_dump(mode="json"):
        raise GrowthError("HEAD_REPLAY_MISMATCH", "重建状态与当前存档头不一致。")
    return {"level": "deterministic_kernel_replay", "run_id": run_id, "through_seq": cursor,
            "events_checked": total_events, "event_hash": event_hash, "operations_replayed": operations,
            "control_transitions_reconstructed": controls, "baseline_graph_commit": baseline_commit,
            "current_head_matches": True, "operational_replay": True, "semantic_replay": False,
            "boundary": "frozen provider outputs and work records; literary meaning is not verified"}
