"""Read original Markdown seeds; initialize live uses, not permanent personas."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .domain import Binding, Continuation, Event, Expression, Limits, Position, Run, Snapshot, Use
from .kernel import audit_for, frame_for, use_id


def read_presets(root: Path) -> list[dict]:
    result = []
    for manifest in sorted((root / "presets").glob("*/preset.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        positions = []
        for meta in data["positions"]:
            position_id = meta["id"]
            if not position_id.isidentifier():
                raise ValueError("预设角色标识无效。")
            seed = (manifest.parent / "characters" / position_id / "character.md").read_text(encoding="utf-8")
            positions.append({**meta, "seed": seed})
        result.append({**data, "positions": positions})
    return result


def initialize(preset: dict, provider: str, limits: Limits) -> Snapshot:
    positions = [Position.model_validate(p) for p in preset["positions"]]
    state = Snapshot(
        run=Run(id=uuid4().hex, title=preset["name"], provider=provider, limits=limits),
        positions=positions,
        expressions=[],
        uses=[],
        continuations=[],
    )
    for position in positions:
        expression_id = f"seed-{position.id}"
        state.expressions.append(
            Expression(
                id=expression_id, text=position.seed, created_revision=0, source_position_id=position.id
            )
        )
        state.uses.append(
            Use(
                id=use_id(position.id, expression_id, "主体背景"),
                position_id=position.id,
                expression_id=expression_id,
                role="主体背景",
                bindings=[
                    Binding(
                        source_position_id=position.id,
                        source_expression_id=expression_id,
                        value=position.seed,
                        anchor=position.seed,
                        revision=0,
                    )
                ],
                revision=0,
            )
        )
        state.continuations.append(Continuation(position_id=position.id, frame_revision=0))
    for initial in preset["expressions"]:
        state.expressions.append(Expression(id=initial["id"], text=initial["text"], created_revision=0))
        for position in positions:
            state.uses.append(
                Use(
                    id=use_id(position.id, initial["id"], "共同生活"),
                    position_id=position.id,
                    expression_id=initial["id"],
                    role="共同生活",
                    bindings=[
                        Binding(
                            source_position_id=position.id,
                            source_expression_id=initial["id"],
                            value=initial["text"],
                            anchor=initial["text"],
                            revision=0,
                        )
                    ],
                    revision=0,
                )
            )
    for continuation in state.continuations:
        frame = frame_for(state, continuation.position_id)
        continuation.condition_fingerprint = frame.condition_fingerprint
        continuation.dependencies = [u["id"] for u in frame.active_uses]
    state.audit = audit_for(state)
    state.events.append(
        Event(
            id=uuid4().hex,
            type="created",
            revision=0,
            data={
                "preset_id": preset["id"],
                "schema_version": 1,
                "initial": state.model_dump(exclude={"events"}),
            },
        )
    )
    return state
