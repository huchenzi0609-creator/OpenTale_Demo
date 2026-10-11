"""Deterministic, resumable plans for actual model method applications.

The compiler's organization trace does not execute a model.  This module builds
the distinct postorder applications a runner must execute before drafting or
rendering.  It performs no HTTP calls, billing, or narrative publication.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .growth_kernel import GrowthError
from .growth_methods import _program_refs, inspect_method
from .growth_store import GrowthStore, digest
from .growth_types import GraphState, ObjectRef, SourceSpan, canonical_scope


def _key(ref: ObjectRef) -> tuple[str, str, str]:
    return ref.kind, ref.object_id, ref.version


def _unique(refs: list[ObjectRef]) -> list[ObjectRef]:
    return list({_key(ref): ref for ref in refs}.values())


def _visible(store: GrowthStore, state: GraphState, scope: str, ref: ObjectRef) -> dict:
    row = store.get(state.run_id, ref.kind, ref.object_id, version=ref.version,
                    at_commit=state.graph_commit if ref.kind in {"node", "link"} else None,
                    scope_id=scope)
    if row is None:
        raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法调用的精确材料在此位置不可见。")
    return row


def plan_invocations(
    store: GrowthStore, state: GraphState, scope: str, method_ref: ObjectRef | dict,
    input_refs: list[ObjectRef | dict], *, context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze ordered actual applications, including a plain method's root.

    ``context`` contains the actual task (stage, purpose, supplied ranges, etc.),
    not a work sequence or an ever-changing clock.  Equivalent exact methods,
    inputs and task contexts share a fingerprint and a completed model result.
    Child results are dependencies of the caller, not world events.  Version
    history is validated but never recursively executed or forwarded as rules.
    """
    scope = canonical_scope(state, scope)
    root = ObjectRef.model_validate(method_ref)
    initial = _program_refs([ObjectRef.model_validate(r).model_dump() for r in input_refs])
    task = deepcopy(context or {})
    frames: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    active: set[tuple[str, str, str]] = set()
    completed: dict[tuple, dict[str, Any]] = {}
    max_depth = 0
    stack: list[dict[str, Any]] = [{"ref": root, "inputs": initial, "depth": 0, "path": "0"}]

    def node_ref(node_id: str) -> ObjectRef:
        if not isinstance(node_id, str):
            raise GrowthError("METHOD_PROGRAM", "方法选材必须使用节点标识。")
        row = store.get(state.run_id, "node", node_id, at_commit=state.graph_commit, scope_id=scope)
        if row is None:
            raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法选中的材料在此位置不可见。")
        return ObjectRef(kind="node", object_id=node_id, version=row["version"])

    while stack:
        current = stack[-1]
        if "method" not in current:
            ref = current["ref"]
            max_depth = max(max_depth, current["depth"])
            if _key(ref) in active:
                applications.append({"path": current["path"], "method_ref": ref.model_dump(),
                                     "depth": current["depth"], "status": "suspended",
                                     "reason": "active_method_cycle"})
                stack.pop()
                continue
            method = inspect_method(store, state, scope, ref)
            program = method.get("program") or {}
            if not isinstance(program, dict):
                raise GrowthError("METHOD_PROGRAM", "方法组织程序格式无效。")
            inputs = _unique([*current["inputs"], *_program_refs(program.get("inputs", []))])
            for evidence in inputs:
                _visible(store, state, scope, evidence)
            application_key = (_key(ref), tuple(_key(r) for r in inputs))
            if application_key in completed:
                reused = completed[application_key]
                applications.append({"path": current["path"], "frame_id": reused["frame_id"],
                                     "method_ref": ref.model_dump(), "depth": current["depth"],
                                     "status": "reused"})
                stack.pop()
                if stack:
                    stack[-1]["children"].append(reused)
                continue
            for evidence in method.get("source_refs", []):
                _visible(store, state, scope, ObjectRef.model_validate(evidence))
            calls = program.get("calls", [])
            if not isinstance(calls, list):
                raise GrowthError("METHOD_PROGRAM", "方法组合调用必须使用列表。")
            validated = []
            for call in calls:
                if (not isinstance(call, dict) or "method_ref" not in call
                        or not call.keys() <= {"method_ref", "input_refs"}):
                    raise GrowthError("METHOD_PROGRAM", "方法组合调用格式无效。")
                callee = _program_refs([call["method_ref"]])[0]
                if callee.kind != "method":
                    raise GrowthError("METHOD_REF_KIND", "组合调用只能使用精确方法版本。")
                validated.append((callee, _program_refs(call.get("input_refs", []))))
            current.update(method=method, program=program, inputs=inputs, application_key=application_key,
                           calls=validated, next_call=0, children=[])
            active.add(_key(ref))
        if current["next_call"] < len(current["calls"]):
            index = current["next_call"]
            current["next_call"] += 1
            callee, call_inputs = current["calls"][index]
            stack.append({"ref": callee, "inputs": call_inputs, "depth": current["depth"] + 1,
                          "path": f"{current['path']}.{index}"})
            continue

        inherited = [ObjectRef.model_validate(r) for child in current["children"]
                     for r in child["output_node_refs"]]
        effective_inputs = _unique([*current["inputs"], *inherited])
        program = current["program"]
        selected = program.get("node_ids", [r.object_id for r in effective_inputs if r.kind == "node"])
        groups, relations = program.get("groups", {}), program.get("relations", [])
        if not isinstance(selected, list) or not isinstance(groups, dict) or not isinstance(relations, list):
            raise GrowthError("METHOD_PROGRAM", "方法选材、分组与关系格式无效。")
        outputs = [node_ref(n) for n in selected]
        for label, ids in groups.items():
            if not isinstance(label, str) or not isinstance(ids, list):
                raise GrowthError("METHOD_PROGRAM", "方法分组格式无效。")
            outputs.extend(node_ref(n) for n in ids)
        for relation in relations:
            if not isinstance(relation, dict):
                raise GrowthError("METHOD_PROGRAM", "方法关系格式无效。")
            outputs.extend(node_ref(relation.get(k)) for k in ("source_id", "target_id"))
        occurrences = program.get("occurrences", [])
        if not isinstance(occurrences, list):
            raise GrowthError("METHOD_PROGRAM", "重复出现位置格式无效。")
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                raise GrowthError("METHOD_PROGRAM", "重复出现位置格式无效。")
            outputs.append(node_ref(occurrence.get("node_id")))
        # Explicit selection makes those exact materials available to this
        # application; this does not publish them or invent a result.
        effective_inputs = _unique([*effective_inputs, *outputs])
        children = list({child["frame_id"]: child for child in current["children"]}.values())
        identity = {"run_id": state.run_id, "scope_id": scope, "method_ref": current["ref"].model_dump(),
                    "input_refs": [r.model_dump() for r in effective_inputs], "context": task,
                    "child_fingerprints": [child["fingerprint"] for child in children]}
        fingerprint = digest(identity)
        frame = {"frame_id": "invoke-" + fingerprint[:32], "fingerprint": fingerprint,
                 "method_ref": current["ref"].model_dump(), "input_refs": identity["input_refs"],
                 "child_frame_ids": [child["frame_id"] for child in children],
                 "output_node_refs": [r.model_dump() for r in _unique(outputs)],
                 "depth": current["depth"], "status": "pending"}
        frames.append(frame)
        applications.append({"path": current["path"], "frame_id": frame["frame_id"],
                             "method_ref": frame["method_ref"], "depth": current["depth"], "status": "pending"})
        completed[current["application_key"]] = frame
        active.remove(_key(current["ref"]))
        stack.pop()
        if stack:
            stack[-1]["children"].append(frame)
    suspended = [a for a in applications if a["status"] == "suspended"]
    return {"plan_id": "invocation-plan-" + digest({"frames": frames, "context": task,
             "applications": applications})[:32], "scope_id": scope, "root_ref": root.model_dump(),
            "root_frame_id": frames[-1]["frame_id"], "frames": frames, "applications": applications,
            "context": task, "max_call_depth": max_depth,
            "status": "suspended" if suspended else "ready", "suspended": suspended}


def _result_projection(store: GrowthStore, state: GraphState, scope: str, frame: dict, result: dict) -> dict:
    if result.get("fingerprint") != frame["fingerprint"] or result.get("method_ref") != frame["method_ref"]:
        raise GrowthError("METHOD_INVOCATION_STALE", "方法调用结果与当前精确输入不一致。")
    if not result.get("id") or not result.get("version"):
        raise GrowthError("METHOD_INVOCATION_REQUIRED", "方法调用结果尚未持久化。")
    ref = ObjectRef(kind="invocation", object_id=result["id"], version=result["version"])
    saved = _visible(store, state, scope, ref)
    if saved.get("fingerprint") != frame["fingerprint"] or saved.get("method_ref") != frame["method_ref"]:
        raise GrowthError("METHOD_INVOCATION_STALE", "保存的调用结果与当前精确输入不一致。")
    value = {"ref": ref.model_dump(), "method_ref": frame["method_ref"], "fingerprint": frame["fingerprint"],
             "used_input_refs": frame["input_refs"], "child_result_refs": saved.get("child_result_refs", []),
             "used_source_spans": deepcopy(saved.get("used_source_spans", [])),
             "status": "proposal_not_world_fact"}
    if saved.get("text_ref"):
        value["text"] = store.read_text(saved["text_ref"])
    elif saved.get("text"):
        value["text"] = saved["text"]
    return value


def _source_span(store: GrowthStore, state: GraphState, scope: str, source: dict) -> dict:
    node = _visible(store, state, scope, ObjectRef(kind="node", object_id=source["id"], version=source["version"]))
    start, end = source.get("start", 0), source.get("end", len(source["text"]))
    if store.read_text(node["text_ref"], start, end) != source["text"]:
        raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "交付给方法的正文与精确来源区间不一致。")
    return SourceSpan(node_id=node["id"], node_version=node["version"], text_ref=node["text_ref"],
                      start=start, end=end).model_dump()


def _span_source(store: GrowthStore, state: GraphState, scope: str, raw: dict) -> dict:
    span = SourceSpan.model_validate(raw)
    node = _visible(store, state, scope, ObjectRef(kind="node", object_id=span.node_id, version=span.node_version))
    if node["text_ref"] != span.text_ref.model_dump():
        raise GrowthError("METHOD_SOURCE_UNAVAILABLE", "方法实际读取的正文版本不匹配。")
    return {"id": node["id"], "version": node["version"], "start": span.start, "end": span.end,
            "text": store.read_text(span.text_ref, span.start, span.end),
            "full_length": span.text_ref.length, "partial": span.start > 0 or span.end < span.text_ref.length}


def _add_source(sources: list[dict], source: dict) -> None:
    # Existing delivery ranges are authoritative. Reusing a node never expands
    # its selected excerpt into the complete source on a subsequent application.
    if not any(row["id"] == source["id"] and row["version"] == source["version"] for row in sources):
        sources.append(source)


def invocation_frame(
    store: GrowthStore, state: GraphState, scope: str, plan: dict, frame_id: str,
    results: dict[str, dict], *, base_frame: dict | None = None,
) -> dict[str, Any]:
    """Deliver this method and actual child results, never all ancestor rules.

    Supplied source excerpts retain their ranges. Drafting may retrieve the
    selected exact node inputs; rendering keeps unvisited inputs as metadata.
    """
    scope = canonical_scope(state, scope)
    if plan["scope_id"] != scope:
        raise GrowthError("METHOD_OWNER", "方法调用计划属于另一工作位置。")
    if plan["status"] == "suspended":
        raise GrowthError("METHOD_INVOCATION_SUSPENDED", "循环调用已挂起，请先修订方法。")
    by_id = {frame["frame_id"]: frame for frame in plan["frames"]}
    if frame_id not in by_id:
        raise GrowthError("METHOD_INVOCATION_REQUIRED", "此调用不属于冻结计划。")
    frame = by_id[frame_id]
    children = []
    for child_id in frame["child_frame_ids"]:
        if child_id not in results:
            raise GrowthError("METHOD_INVOCATION_REQUIRED", "子方法尚未完成实际调用。")
        children.append(_result_projection(store, state, scope, by_id[child_id], results[child_id]))
    method = inspect_method(store, state, scope, frame["method_ref"])
    value = deepcopy(base_frame or {})
    context = plan["context"]
    mode = context.get("mode", context.get("stage", "draft"))
    sources = value.setdefault("sources", [])
    evidence = []
    for raw in frame["input_refs"]:
        ref = ObjectRef.model_validate(raw)
        row = _visible(store, state, scope, ref)
        item = {**raw, "object": row}
        if ref.kind == "node" and mode == "draft":
            stop = min(row["text_ref"]["length"], 2500)
            _add_source(sources, {"id": row["id"], "version": row["version"], "start": 0, "end": stop,
                "text": store.read_text(row["text_ref"], 0, stop),
                "full_length": row["text_ref"]["length"], "partial": stop < row["text_ref"]["length"]})
        elif ref.kind == "method":
            item["text"] = row["instruction"]
        elif ref.kind != "node" and row.get("text_ref"):
            text = store.read_text(row["text_ref"])
            item.update(text=text[:2500], full_length=len(text), partial=len(text) > 2500)
        evidence.append(item)
    spans = [_source_span(store, state, scope, source) for source in sources]
    if mode == "draft":
        for child in children:
            for span in child["used_source_spans"]:
                _span_source(store, state, scope, span)
                if span not in spans:
                    spans.append(span)
    value.update(scope_id=scope, method=method, method_evidence=evidence,
                 invocation_context={"plan_id": plan["plan_id"], "frame_id": frame_id,
                     "mode": mode,
                     "goal": context.get("goal", context.get("direction", context.get("purpose", ""))),
                     "depth": frame["depth"], "root_ref": plan["root_ref"], "task": context},
                 invocation_children=children, invocation_source_spans=spans)
    return value


def attach_invocation_result(
    store: GrowthStore, state: GraphState, scope: str, plan: dict,
    results: dict[str, dict], base_frame: dict,
) -> dict[str, Any]:
    """Attach a saved root application to the actual draft/render input."""
    scope = canonical_scope(state, scope)
    if plan["scope_id"] != scope or plan["status"] == "suspended":
        raise GrowthError("METHOD_INVOCATION_SUSPENDED", "当前方法调用计划尚不能进入生成。")
    root_id = plan["root_frame_id"]
    if root_id not in results:
        raise GrowthError("METHOD_INVOCATION_REQUIRED", "当前方法尚未完成实际调用。")
    root_frame = next(frame for frame in plan["frames"] if frame["frame_id"] == root_id)
    value = deepcopy(base_frame)
    value["method_invocation"] = _result_projection(store, state, scope, root_frame, results[root_id])
    value["method_invocation"]["max_call_depth"] = plan["max_call_depth"]
    mode = plan["context"].get("mode", plan["context"].get("stage", "draft"))
    if mode == "draft":
        sources = value.setdefault("sources", [])
        for span in value["method_invocation"]["used_source_spans"]:
            _add_source(sources, _span_source(store, state, scope, span))
    return value
