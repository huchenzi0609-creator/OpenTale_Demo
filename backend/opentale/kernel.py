"""Deterministic shared-use rewrites and source-blind frame materialization."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from .domain import Binding, Expression, GenerationFrame, Presentation, Proposal, Snapshot, StorySegment, Use


class KernelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, False


def use_id(position_id: str, expression_id: str, role: str) -> str:
    suffix = hashlib.sha256(role.encode()).hexdigest()[:12]
    return f"use-{position_id}-{expression_id}-{suffix}"


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def frame_for(state: Snapshot, position_id: str) -> GenerationFrame:
    position = next(p for p in state.positions if p.id == position_id)
    continuation = next(c for c in state.continuations if c.position_id == position_id)
    expressions = {e.id: e for e in state.expressions}
    active = []
    for use in sorted(state.uses, key=lambda u: u.id):
        if use.position_id != position_id:
            continue
        values = sorted({b.value for b in use.bindings})
        # A biography seed is a live binding. After rebind, do not silently re-inject the original profile.
        text = "\n".join(values) if use.role == "主体背景" else expressions[use.expression_id].text
        active.append(
            {
                "id": use.id,
                "expression_id": use.expression_id,
                "text": text,
                "role": use.role,
                "values": values,
            }
        )
    digest = fingerprint(active)
    return GenerationFrame(
        position_id=position.id,
        name=position.name,
        revision=state.run.revision,
        seed_text=continuation.seed_text,
        active_uses=active,
        condition_fingerprint=digest,
        recent_surface=([continuation.surface] if continuation.surface else []),
        peers=[{"id": p.id, "name": p.name} for p in state.positions if p.id != position_id],
        peer_interfaces=[
            {"position_id": u.position_id, "expression_id": u.expression_id, "role": u.role}
            for u in sorted(state.uses, key=lambda u: u.id)
            if u.position_id != position_id
        ],
    )


def install_batch(state: Snapshot, proposals: list[Proposal], base_revision: int) -> Snapshot:
    """Validate entirely, then replace scoped target groups on a detached snapshot.

    Arrival order has no effect. All writers of one role survive the same installation.
    Scope is position + expression + open role, never a universal fact key.
    """
    if base_revision != state.run.revision:
        raise KernelError("STALE_PROPOSAL", "该局部的条件已改变，请依据新条件重新生成。")
    positions = {p.id for p in state.positions}
    writers = [p.position_id for p in proposals]
    if len(writers) != len(set(writers)) or set(writers) != positions:
        raise KernelError("WINDOW_INCOMPLETE", "本窗口必须包含每个参与位置的一份表达。")
    for proposal in proposals:
        validate_proposal(state, proposal)
    result = state.model_copy(deep=True)
    revision = base_revision + 1
    groups: dict[tuple[str, str, str], list[Binding]] = defaultdict(list)
    batch_trace = []
    before = {p.id: frame_for(state, p.id).condition_fingerprint for p in state.positions}
    for proposal in sorted(proposals, key=lambda p: p.position_id):
        source_expression_id = f"expr-{revision}-{proposal.position_id}"
        result.expressions.append(
            Expression(
                id=source_expression_id,
                text=proposal.text,
                created_revision=revision,
                source_position_id=proposal.position_id,
            )
        )
        for operation in proposal.operations:
            key = (operation.target_position_id, operation.expression_id, operation.role)
            groups[key].append(
                Binding(
                    source_position_id=proposal.position_id,
                    source_expression_id=source_expression_id,
                    value=operation.value,
                    anchor=operation.anchor,
                    revision=revision,
                )
            )
            batch_trace.append(
                {
                    "source_position_id": proposal.position_id,
                    **operation.model_dump(),
                    "source_expression_id": source_expression_id,
                }
            )
    uses = {(u.position_id, u.expression_id, u.role): u for u in result.uses}
    for key, bindings in sorted(groups.items()):
        # Correlated bindings remain separately attributed, including incompatible values.
        bindings = sorted(bindings, key=lambda b: (b.source_position_id, b.value, b.anchor))
        uses[key] = Use(
            id=use_id(*key),
            position_id=key[0],
            expression_id=key[1],
            role=key[2],
            bindings=bindings,
            revision=revision,
        )
    result.uses = sorted(uses.values(), key=lambda u: u.id)
    result.run.revision = revision
    proposal_map = {p.position_id: p for p in proposals}
    for continuation in result.continuations:
        continuation.seed_text = proposal_map[continuation.position_id].text
        continuation.frame_revision = revision
        continuation.status = "pending"
        frame = frame_for(result, continuation.position_id)
        continuation.condition_fingerprint = frame.condition_fingerprint
        continuation.dependencies = [u["id"] for u in frame.active_uses]
    result.audit = audit_for(result)
    if not any(
        len({b.source_position_id for b in bindings if b.source_position_id != key[0]}) > 1
        for key, bindings in groups.items()
    ):
        raise KernelError("JOINT_DEPENDENCY_MISSING", "本次表达尚未共同改变同一个活跃用法，请重新构想局部。")
    result.audit["last_batch"] = batch_trace
    result.audit["condition_changes"] = [
        {
            "position_id": p.id,
            "before": before[p.id],
            "after": frame_for(result, p.id).condition_fingerprint,
            "changed": before[p.id] != frame_for(result, p.id).condition_fingerprint,
        }
        for p in result.positions
    ]
    changed_positions = {c["position_id"] for c in result.audit["condition_changes"] if c["changed"]}
    for proposal in proposals:
        peer_targets = {
            o.target_position_id for o in proposal.operations if o.target_position_id != proposal.position_id
        }
        if not peer_targets & changed_positions:
            raise KernelError("DEPENDENCY_MISSING", "该表达没有改变任何同伴的实际条件。")
    return result


def validate_proposal(state: Snapshot, proposal: Proposal) -> None:
    positions = {p.id for p in state.positions}
    expression_ids = {e.id for e in state.expressions}
    if proposal.position_id not in positions or proposal.based_on_revision != state.run.revision:
        raise KernelError("STALE_PROPOSAL", "表达依据的位置或条件版本已过时。")
    peer_effect = False
    for operation in proposal.operations:
        if operation.target_position_id not in positions or operation.expression_id not in expression_ids:
            raise KernelError("REFERENCE_INVALID", "表达引用了不存在的位置或片段。")
        if operation.anchor not in proposal.text:
            raise KernelError("ANCHOR_INVALID", "关系操作必须锚定本次未完成表达中的原文。")
        peer_effect |= operation.target_position_id != proposal.position_id
    if not peer_effect:
        raise KernelError("DEPENDENCY_MISSING", "本次转用须进入另一位置的实际条件。")


def audit_for(state: Snapshot) -> dict[str, Any]:
    shared: dict[str, set[str]] = defaultdict(set)
    edges, joint = [], []
    for use in state.uses:
        shared[use.expression_id].add(use.position_id)
        sources = sorted(
            {b.source_position_id for b in use.bindings if b.source_position_id != use.position_id}
        )
        for source in sources:
            edges.append(
                {
                    "source_position_id": source,
                    "target_position_id": use.position_id,
                    "expression_id": use.expression_id,
                    "role": use.role,
                    "use_id": use.id,
                }
            )
        if len(sources) > 1:
            joint.append(
                {
                    "position_id": use.position_id,
                    "expression_id": use.expression_id,
                    "role": use.role,
                    "sources": sources,
                    "use_id": use.id,
                }
            )
    return {
        "shared_expressions": [
            {"expression_id": key, "position_ids": sorted(value)}
            for key, value in sorted(shared.items())
            if len(value) > 1
        ],
        "effective_edges": sorted(edges, key=lambda e: (e["source_position_id"], e["use_id"])),
        "joint_targets": sorted(joint, key=lambda j: j["use_id"]),
        "last_batch": [],
    }


def without_source(state: Snapshot, target_id: str, source_id: str) -> tuple[Snapshot, list[str]]:
    result = state.model_copy(deep=True)
    changed = []
    for use in result.uses:
        if use.position_id != target_id:
            continue
        kept = [b for b in use.bindings if b.source_position_id != source_id]
        if len(kept) != len(use.bindings):
            use.bindings = kept
            changed.append(use.id)
    result.uses = [u for u in result.uses if u.bindings]
    return result, changed


def install_presentation(state: Snapshot, presentation: Presentation) -> None:
    positions = {p.id for p in state.positions}
    allowed = {u.expression_id for u in state.uses}
    shared = {e["expression_id"] for e in audit_for(state)["shared_expressions"]}
    segments = {s.id for s in state.story}
    if (
        presentation.focus_position_id not in positions
        or not set(presentation.expression_ids) <= allowed
        or not set(presentation.expression_ids) & shared
    ):
        raise KernelError("PRESENTATION_REFERENCE_INVALID", "呈现须承接当前有效的共同表达。")
    if not set(presentation.reinterprets) <= segments:
        raise KernelError("PRESENTATION_REFERENCE_INVALID", "返回的读法引用了尚未呈现的片段。")
    segment = StorySegment(
        id=f"story-{state.run.revision}",
        revision=state.run.revision,
        text=presentation.text,
        focus_position_id=presentation.focus_position_id,
        expression_ids=presentation.expression_ids,
        reinterprets=presentation.reinterprets,
    )
    state.story.append(segment)
    state.reading.focus_position_id = presentation.focus_position_id
    state.reading.open_relations = [
        {
            "position_id": joint["position_id"],
            "expression_id": joint["expression_id"],
            "role": joint["role"],
            "position_ids": sorted(
                {u.position_id for u in state.uses if u.expression_id == joint["expression_id"]}
            ),
        }
        for joint in audit_for(state)["joint_targets"]
    ]
    for previous_id in presentation.reinterprets:
        state.reading.interpretations.append(
            {
                "segment_id": previous_id,
                "through_segment_id": segment.id,
                "focus_position_id": presentation.focus_position_id,
                "revision": state.run.revision,
            }
        )


def semantic_projection(state: Snapshot) -> dict:
    return {
        key: state.model_dump()[key]
        for key in ("positions", "expressions", "uses", "continuations", "story", "reading")
    }
