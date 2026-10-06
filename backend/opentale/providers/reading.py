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
        if shared.intersection(use["expression_id"] for use in candidate.get("active_uses", []))
    ]
    required = required_focus(frame)
    if required:
        candidates = [candidate for candidate in candidates if candidate["position_id"] == required]
    if not candidates:
        raise ProviderError("READING_FRAME_INVALID", "当前阅读位置没有可共同承接的有效表达。")
    return candidates


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
    if (
        candidate is None
        or not referenced <= scopes
        or not referenced.intersection(shared)
        or not set(presentation.reinterprets) <= previous
    ):
        raise ProviderError(
            "MODEL_OUTPUT_INVALID", "模型提出了无效的阅读承接。", True, usage=usage,
            feedback=OutputFeedback("READING_INVALID", ("READING_SCOPE_INVALID",)),
        )
