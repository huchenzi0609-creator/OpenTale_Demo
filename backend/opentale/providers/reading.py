"""Reading constraints derived only from live scopes and this reading's history."""

from __future__ import annotations

from collections import defaultdict

from opentale.domain import Presentation, ReadingFrame

from .base import ProviderError
from .feedback import OutputFeedback


def shared_expression_ids(frame: ReadingFrame, *, legacy_letter: bool = False) -> set[str]:
    participants: dict[str, set[str]] = defaultdict(set)
    for candidate in frame.candidates:
        for use in candidate.get("active_uses", []):
            participants[use["expression_id"]].add(candidate["position_id"])
    shared = {expression_id for expression_id, positions in participants.items() if len(positions) > 1}
    # Historical unit frames used one letter view. Actual reading frames contain every position.
    if legacy_letter and len(frame.candidates) == 1 and "e-letter" in participants:
        shared.add("e-letter")
    return shared


def required_focus(frame: ReadingFrame) -> str | None:
    if not frame.previous_segments and frame.start_position_id:
        return frame.start_position_id
    return frame.required_focus_position_id


def eligible_candidates(frame: ReadingFrame, *, legacy_letter: bool = False) -> list[dict]:
    shared = shared_expression_ids(frame, legacy_letter=legacy_letter)
    candidates = [
        candidate for candidate in frame.candidates
        if frame.reflection is not None
        or shared.intersection(use["expression_id"] for use in candidate.get("active_uses", []))
    ]
    required = required_focus(frame)
    if required:
        candidates = [candidate for candidate in candidates if candidate["position_id"] == required]
    if not candidates:
        raise ProviderError("READING_FRAME_INVALID", "当前阅读位置没有可共同承接的有效表达。")
    return candidates


def reading_table(frame: ReadingFrame) -> list[dict]:
    """Freeze legal focus/expression pairs into handles owned by this one request.

    The expression index is local to its focus row. It cannot be looked up in a
    different focus, another reading, or a later world revision.
    """
    shared = shared_expression_ids(frame)
    return [
        {
            "focus_index": index,
            "position_id": candidate["position_id"],
            "name": candidate["name"],
            "expressions": [
                {"expression_index": expression_index, "expression_id": expression_id,
                 "shared": expression_id in shared}
                for expression_index, expression_id in enumerate(sorted({
                    use["expression_id"] for use in candidate.get("active_uses", [])
                }))
            ],
        }
        for index, candidate in enumerate(eligible_candidates(frame))
    ]


def history_table(frame: ReadingFrame) -> list[dict]:
    """Only segments actually provided by this reading, in their frozen order."""
    return [{"history_index": index, "segment_id": segment["id"]}
            for index, segment in enumerate(frame.previous_segments)]


def invalid_reading(reason: str, *, usage: tuple[int, int] | None = None) -> None:
    raise ProviderError(
        "MODEL_OUTPUT_INVALID", f"模型提出了无效的阅读承接（{reason}）。", True, usage=usage,
        feedback=OutputFeedback("READING_INVALID", (reason,)),
    )


def compile_reading_references(
    frame: ReadingFrame,
    focus_index: int,
    expression_indices: list[int],
    reinterprets_indices: list[int],
    *,
    usage: tuple[int, int] | None = None,
) -> dict:
    """Compile exact selected rows; never infer names, drop a reference or rebind."""
    table = reading_table(frame)
    if type(focus_index) is not int or not 0 <= focus_index < len(table):
        invalid_reading("READING_FOCUS_INVALID", usage=usage)
    focus = table[focus_index]
    expressions = focus["expressions"]
    if any(type(index) is not int or not 0 <= index < len(expressions) for index in expression_indices):
        invalid_reading("READING_EXPRESSION_INVALID", usage=usage)
    history = history_table(frame)
    if any(type(index) is not int or not 0 <= index < len(history) for index in reinterprets_indices):
        invalid_reading("READING_HISTORY_INVALID", usage=usage)
    return {
        "focus_position_id": focus["position_id"],
        "expression_ids": [expressions[index]["expression_id"] for index in expression_indices],
        "reinterprets": [history[index]["segment_id"] for index in reinterprets_indices],
    }


def validate_presentation(
    frame: ReadingFrame,
    presentation: Presentation,
    *,
    usage: tuple[int, int] | None = None,
    legacy_letter: bool = False,
) -> None:
    eligible = {candidate["position_id"]: candidate for candidate in eligible_candidates(
        frame, legacy_letter=legacy_letter,
    )}
    candidate = eligible.get(presentation.focus_position_id)
    scopes = {use["expression_id"] for use in candidate.get("active_uses", [])} if candidate else set()
    referenced = set(presentation.expression_ids)
    previous = {segment["id"] for segment in frame.previous_segments}
    shared = shared_expression_ids(frame, legacy_letter=legacy_letter)
    if candidate is None:
        invalid_reading("READING_FOCUS_INVALID", usage=usage)
    if not referenced <= scopes:
        invalid_reading("READING_EXPRESSION_INVALID", usage=usage)
    if frame.reflection is None and not referenced.intersection(shared):
        invalid_reading("READING_SHARED_REQUIRED", usage=usage)
    if not set(presentation.reinterprets) <= previous:
        invalid_reading("READING_HISTORY_INVALID", usage=usage)
