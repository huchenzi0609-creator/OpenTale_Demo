"""Deterministic full-value lexical and relationship retrieval, including Chinese."""

from __future__ import annotations

import json
import re
from collections import Counter

from .policy_types import ObjectRef


def terms(text: str) -> Counter[str]:
    result: Counter[str] = Counter()
    lowered = text.casefold()
    result.update(re.findall(r"[a-z0-9_]+", lowered))
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        if len(run) == 1:
            result.update(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def references(value: object) -> set[str]:
    """Only declared reference fields count as graph edges; arbitrary prose does not."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"inputs", "about", "evidence", "selected", "material_ids"} and isinstance(item, list):
                found.update(ref for ref in item if isinstance(ref, str))
            elif key in {"object_id", "revises"} and isinstance(item, str):
                found.add(item)
            if isinstance(item, (dict, list)):
                found.update(references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(references(item))
    return found


def retrieve(
    objects: dict[str, dict], query: str, *, kinds: list[str] | None = None,
    related_to: list[ObjectRef] | None = None, limit: int | None = None,
) -> list[str]:
    """No recency tail or implicit result cap; explicit limit is a selection operation."""
    needle = terms(query)
    anchors = {ref.object_id for ref in related_to or []}
    scores: list[tuple[int, str]] = []
    for identifier, row in objects.items():
        if kinds and row.get("kind") not in kinds:
            continue
        linked = identifier in anchors or bool(references(row) & anchors)
        if anchors and not linked:
            continue
        haystack = "\n".join(str(row.get(key, "")) for key in ("text", "title", "instruction"))
        if row.get("program"):
            haystack += json.dumps(row["program"], ensure_ascii=False, sort_keys=True)
        count = terms(haystack)
        score = sum(min(weight, count[token]) for token, weight in needle.items())
        if query.casefold() in haystack.casefold():
            score += max(1, len(needle)) * 4
        if score or linked:
            scores.append((score, identifier))
    scores.sort(key=lambda entry: (-entry[0], entry[1]))
    ids = [identifier for _, identifier in scores]
    return ids if limit is None else ids[:limit]
