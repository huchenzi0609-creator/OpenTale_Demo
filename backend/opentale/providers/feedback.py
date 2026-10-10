"""Finite output feedback: codes and schema fields only, never rejected user data."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

_CATEGORIES = frozenset({
    "JSON_SYNTAX", "JSON_TYPE", "SCHEMA_VALIDATION", "PROPOSAL_INVALID", "PROPOSAL_REFERENCE",
    "READING_INVALID", "PROSE_INVALID",
})
_FIELDS = frozenset({
    "text", "fragments", "targets", "target_position_id", "expression_id", "role", "interface_index",
    "position_id", "based_on_revision", "operations", "op", "value", "anchor",
    "focus_position_id", "expression_ids", "reinterprets",
    "focus_index", "expression_indices", "reinterprets_indices",
    "reflection", "methods", "adoptions", "title", "instruction", "object_indices", "order", "limit",
    "revises_index", "scope_index", "method_index", "draft_index", "action",
    "args", "method_id", "method_version", "evaluation_method_id", "evaluation_method_version",
    "trial_id", "trial_version", "node_id", "edge_id",
    "input_ids", "input_refs", "about_ids", "parent_ids", "association_ids", "visit_ids", "id", "kind",
    "object_id", "version", "program", "inputs", "calls", "method_ref", "input_refs", "groups", "relations",
    "source_id", "target_id", "relation", "purpose", "query", "offset", "start", "end", "direction",
    "transition", "reason", "node_ids", "about_refs",
})
_ERROR_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
    "list_type", "too_short", "too_long", "model_type", "int_type", "literal_error",
})
_REASONS = frozenset({
    "INVALID_JSON", "DUPLICATE_FIELD", "NON_JSON_NUMBER", "ROOT_NOT_OBJECT",
    "EMPTY_FRAGMENT", "FRAGMENT_TOO_LONG", "NO_PEER_OPERATION", "TOO_MANY_OPERATIONS",
    "PROPOSAL_TOO_LARGE", "POSITION_MISMATCH", "REVISION_MISMATCH", "TARGET_UNKNOWN", "INTERFACE_UNKNOWN",
    "EXPRESSION_UNAVAILABLE", "ANCHOR_NOT_IN_TEXT", "READING_SCOPE_INVALID",
    "READING_FOCUS_INVALID", "READING_EXPRESSION_INVALID", "READING_HISTORY_INVALID",
    "READING_SHARED_REQUIRED",
    "TECHNICAL_PROSE", "SCHEMA_INVALID", "WRAPPING_TOO_DEEP",
    "REFLECTION_REFERENCE_INVALID",
    "HANDLE_UNKNOWN", "HANDLE_KIND_MISMATCH", "HANDLE_AMBIGUOUS", "HANDLE_VERSION_MISMATCH",
    "METHOD_UNCHANGED",
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
        if self.category == "PROPOSAL_REFERENCE" and (
            len(self.issues) != 1 or type(self.issues[0]) is not str or self.issues[0] not in {
                "TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE", "INTERFACE_UNKNOWN",
            }
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
