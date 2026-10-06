"""Finite output feedback: codes and schema fields only, never rejected user data."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

_CATEGORIES = frozenset({
    "JSON_SYNTAX", "JSON_TYPE", "SCHEMA_VALIDATION", "PROPOSAL_INVALID",
    "READING_INVALID", "PROSE_INVALID",
})
_FIELDS = frozenset({
    "text", "fragments", "targets", "target_position_id", "expression_id", "role",
    "position_id", "based_on_revision", "operations", "op", "value", "anchor",
    "focus_position_id", "expression_ids", "reinterprets",
})
_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
    "list_type", "too_short", "too_long", "model_type", "int_type", "literal_error",
})
_REASONS = frozenset({
    "INVALID_JSON", "DUPLICATE_FIELD", "NON_JSON_NUMBER", "ROOT_NOT_OBJECT",
    "EMPTY_FRAGMENT", "FRAGMENT_TOO_LONG", "NO_PEER_OPERATION", "TOO_MANY_OPERATIONS",
    "PROPOSAL_TOO_LARGE", "POSITION_MISMATCH", "REVISION_MISMATCH", "TARGET_UNKNOWN",
    "EXPRESSION_UNAVAILABLE", "ANCHOR_NOT_IN_TEXT", "READING_SCOPE_INVALID",
    "TECHNICAL_PROSE", "SCHEMA_INVALID", "WRAPPING_TOO_DEEP",
})


@dataclass(frozen=True, slots=True)
class OutputFeedback:
    category: str
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.category) is not str or self.category not in _CATEGORIES
            or type(self.issues) is not tuple or len(self.issues) > 8
        ):
            raise ValueError("Invalid safe output feedback")
        for issue in self.issues:
            if type(issue) is not str or len(issue) > 200:
                raise ValueError("Invalid safe output feedback")
            if issue in _REASONS:
                continue
            if issue.count(":") != 1:
                raise ValueError("Invalid safe output feedback")
            path, error_type = issue.split(":")
            if error_type not in _ERROR_TYPES | {"schema_invalid"}:
                raise ValueError("Invalid safe output feedback")
            if path != "schema" and any(part.removesuffix("[]") not in _FIELDS for part in path.split(".")):
                raise ValueError("Invalid safe output feedback")

    def prompt_data(self) -> dict[str, object]:
        return {"category": self.category, "issues": list(self.issues)}


def schema_feedback(error: ValidationError) -> OutputFeedback:
    issues: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        error_type = detail["type"]
        if error_type not in _ERROR_TYPES:
            error_type = "schema_invalid"
        parts: list[str] = []
        for component in detail["loc"]:
            if type(component) is int and parts:
                parts[-1] += "[]"
            elif isinstance(component, str) and component in _FIELDS:
                parts.append(component)
            else:
                parts = []
                break
        # Unknown field names and custom model labels never cross the boundary.
        path = ".".join(parts) or "schema"
        issue = f"{path}:{error_type}"
        if issue not in issues:
            issues.append(issue)
        if len(issues) == 8:
            break
    return OutputFeedback("SCHEMA_VALIDATION", tuple(issues) or ("SCHEMA_INVALID",))
