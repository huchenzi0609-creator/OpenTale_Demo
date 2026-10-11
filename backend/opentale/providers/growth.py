"""Small-step graph evaluators; only the host executes their validated actions.

The legacy provider owns transport, strict JSON, accounting and safe errors. This
adapter supplies a separate graph-work protocol and never invokes a world tool.
Page reads are real host actions; selected text and feedback are never truncated.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from opentale.capacity import (
    MAX_MODEL_REQUEST_CHARS,
    MAX_PRESENTATION_TEXT_CHARS,
    MAX_REALIZATION_TEXT_CHARS,
)
from opentale.config import Settings
from opentale.growth_capabilities import (
    KINDS,
    CapabilityError,
    capability_table,
    collect_capabilities,
    offered_ids,
    resolve_handle,
)
from opentale.security import contains_credential

from .base import ModelResult, ProviderError
from .deepseek import DeepSeekProvider
from .feedback import OutputFeedback

GrowthStage = Literal["decide", "invoke", "draft", "render", "review"]
Action = Literal[
    "query", "read_page", "inspect", "apply", "revise", "adopt", "try_write",
    "publish", "navigate", "render", "respond", "reflect", "wait",
    "retry_review",
]
PROJECTION_VERSION = "growth-pages-v1"


class GrowthProvider(Protocol):
    async def decide(
        self, frame: dict, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[dict]: ...

    async def draft(
        self, frame: dict, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]: ...

    async def render(
        self, frame: dict, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]: ...

    async def review(
        self, frame: dict, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]: ...

    async def invoke(
        self, frame: dict, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]: ...

    def projection(
        self, frame: dict, stage: GrowthStage, *, repair: OutputFeedback | None = None,
    ) -> dict: ...


class _Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Action
    args: dict


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_REALIZATION_TEXT_CHARS)


class _Rendered(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)


class _Reviewed(BaseModel):
    """Public work evidence, deliberately distinct from fictional prose."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)


class _Invoked(BaseModel):
    """A method's executable proposal for this exact generation context."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)


_ACTIONS = frozenset(_Decision.model_json_schema()["properties"]["action"]["enum"])
_FIELDS = {
    "query": ({"query", "kind"}, {"query", "kind", "offset"}),
    "read_page": ({"kind", "id"}, {"kind", "id", "version", "start", "end"}),
    "inspect": ({"method_id"}, {"method_id", "method_version"}),
    "apply": ({"method_id"}, {"method_id", "method_version"}),
    "adopt": ({"method_id"}, {"method_id", "method_version"}),
    "revise": ({"method_id", "instruction"}, {"method_id", "method_version", "instruction", "input_ids", "input_refs", "purpose", "program"}),
    "try_write": ({"parent_ids"}, {"parent_ids", "direction"}),
    "publish": ({"trial_id"}, {"trial_id", "trial_version", "parent_ids", "association_ids", "relation", "allow_pending_review"}),
    "navigate": ({"node_id"}, {"node_id", "edge_id", "transition", "start", "end"}),
    "render": ({"visit_ids"}, {"visit_ids"}),
    "respond": ({"text"}, {"text", "about_ids", "about_refs", "evaluation_method_id", "evaluation_method_version"}),
    "reflect": ({"method_id"}, {"method_id", "method_version", "about_ids", "about_refs", "purpose"}),
    "retry_review": ({"debt_id"}, {"debt_id"}),
    "wait": (set(), {"reason"}),
}
_FRAME_FIELDS = frozenset({
    "actor_revision", "reading_history",
    "scope_id", "session_id", "role", "graph_commit", "work_seq", "direction", "cursor",
    "frontier", "allowed_actions", "nodes", "methods", "links", "trials", "visits",
    "pending_trial", "pending_candidate", "method", "sources", "responses", "receipts",
    "pages", "page_capabilities", "selected_refs", "source_spans", "visit_ids", "read_seq",
    "observed_graph_commit", "evaluation_method", "window", "entries", "status",
    "parent_ids", "entry_node_ids", "catalog_page",
    "view", "method_evidence", "method_dependencies", "feedback_catalog", "response_page", "selected_node_ids",
    "views", "method_work", "reading_work", "capabilities", "method_execution", "method_cache",
    "iteration_work", "work_protocol", "review_targets", "review_order", "purpose",
    "invocation_context", "invocation_children", "method_invocation", "evidence_context", "world_frontier",
    "review_debt", "invocations",
})
_PRIVATE_KEYS = frozenset({
    "api_key", "authorization", "Authorization", "base_url", "endpoint", "settings",
    "reasoning", "reasoning_content", "hidden_reasoning", "cot", "CoT", "creator_provenance",
    "condition_fingerprint", "raw_error", "provider_error_body",
    "creator_id", "source_position_id", "source_expression_id",
})


def _invalid(*, usage: tuple[int, int] | None = None, field: str | None = None,
             issue: str = "SCHEMA_INVALID") -> ProviderError:
    return ProviderError(
        "MODEL_OUTPUT_INVALID", "工作动作的字段或可用句柄无效，请依据当前目录重新选择。", True,
        usage=usage, feedback=OutputFeedback("SCHEMA_VALIDATION", (
            *((f"{field}:schema_invalid",) if field else ()), issue,
        )),
    )


def _catalog(frame: dict, name: str) -> list[dict]:
    rows = frame.get(name, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProviderError("GROWTH_FRAME_INVALID", "当前工作目录格式无效。")
    return rows


def _pending_review_debts(frame: dict) -> list[dict]:
    pending = [debt for debt in _catalog(frame, "review_debt")
               if type(debt.get("status")) is str and debt["status"] in {"pending", "failed"}
               and _string(debt.get("id"))]
    if any(type(debt.get("targets", [])) is not list
           or any(not isinstance(target, dict) for target in debt.get("targets", [])) for debt in pending):
        raise ProviderError("GROWTH_FRAME_INVALID", "当前待补评议目录格式无效。")
    return pending


def _trial_review_pending(frame: dict, trial_id: str) -> bool:
    return any(target.get("kind") == "trial" and target.get("object_id") == trial_id
               for debt in _pending_review_debts(frame)
               for target in debt.get("targets", []) if isinstance(target, dict))


def _ids(frame: dict, name: str) -> set[str]:
    return {row["id"] for row in _catalog(frame, name) if type(row.get("id")) is str}


def _visible_rows(frame: dict, kind: Literal["link", "view"]) -> list[dict]:
    """Full structural handles can arrive in the initial catalog or a later page."""
    rows = list(_catalog(frame, "links" if kind == "link" else "views"))
    if kind == "view" and isinstance(frame.get("view"), dict):
        rows.append(frame["view"])
    for page in _catalog(frame, "pages"):
        if page.get("kind") != kind:
            continue
        if isinstance(page.get("items"), list):
            rows.extend(item for item in page["items"] if isinstance(item, dict))
        for field in ("object", "structure"):
            if isinstance(page.get(field), dict):
                rows.append(page[field])
        if kind == "link" and {"source_id", "target_id"} <= page.keys():
            rows.append(page)
    return rows


def _offered(frame: dict) -> dict[str, set[str]]:
    try:
        return offered_ids(frame)
    except CapabilityError:
        raise ProviderError("GROWTH_FRAME_INVALID", "当前工作句柄目录格式无效。") from None


def _string(value: object, *, maximum: int = 32_000) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= maximum


def _references(value: object, allowed: set[str], *, nonempty: bool = False) -> bool:
    return (
        type(value) is list and (bool(value) or not nonempty) and len(value) <= 256
        and all(type(item) is str and item in allowed for item in value)
        and len(set(value)) == len(value)
    )


def _valid_exact_ref(ref: object, frame: dict, kind: str | None = None) -> bool:
    if (type(ref) is not dict or set(ref) != {"kind", "object_id", "version"}
            or type(ref["kind"]) is not str or ref["kind"] not in KINDS
            or not _string(ref["object_id"]) or not _string(ref["version"])
            or kind is not None and ref["kind"] != kind):
        return False
    try:
        resolve_handle(frame, ref["object_id"], ref["kind"], ref["version"])
    except CapabilityError:
        return False
    return True


def _valid_exact_refs(refs: object, frame: dict) -> bool:
    return (type(refs) is list and len(refs) <= 256
            and all(_valid_exact_ref(ref, frame) for ref in refs)
            and len({(ref["kind"], ref["object_id"], ref["version"]) for ref in refs}) == len(refs))


def _valid_program(program: object, frame: dict) -> bool:
    nodes = _offered(frame)["node"]
    if type(program) is not dict or not program.keys() <= {
        "node_ids", "groups", "relations", "purpose", "inputs", "calls",
    }:
        return False
    if "inputs" in program and not _valid_exact_refs(program["inputs"], frame):
        return False
    calls = program.get("calls", [])
    if type(calls) is not list or len(calls) > 256:
        return False
    for call in calls:
        if (type(call) is not dict or not {"method_ref"} <= call.keys()
                or not call.keys() <= {"method_ref", "input_refs"}
                or not _valid_exact_ref(call["method_ref"], frame, "method")
                or not _valid_exact_refs(call.get("input_refs", []), frame)):
            return False
    if "node_ids" in program and not _references(program["node_ids"], nodes):
        return False
    if "purpose" in program and not _string(program["purpose"], maximum=200):
        return False
    groups = program.get("groups", {})
    if type(groups) is not dict or len(groups) > 64:
        return False
    if any(not _string(label, maximum=80) or not _references(items, nodes) for label, items in groups.items()):
        return False
    relations = program.get("relations", [])
    if type(relations) is not list or len(relations) > 256:
        return False
    for relation in relations:
        if (type(relation) is not dict or not {"source_id", "target_id"} <= relation.keys()
                or not relation.keys() <= {"source_id", "target_id", "relation", "kind"}):
            return False
        if not all(type(relation[field]) is str and relation[field] in nodes for field in ("source_id", "target_id")):
            return False
        if "relation" in relation and not _string(relation["relation"], maximum=200):
            return False
        if "kind" in relation and (type(relation["kind"]) is not str or relation["kind"] not in {"association", "growth"}):
            return False
    return True


def _complete_method(frame: dict, method_id: str) -> dict | None:
    """Compare only full definitions, never shortened method catalog previews."""
    current = frame.get("method")
    if isinstance(current, dict) and current.get("id") == method_id:
        return current
    for page in _catalog(frame, "pages"):
        method = page.get("method")
        if isinstance(method, dict) and method.get("id") == method_id:
            return method
    cache = frame.get("method_cache", [])
    if isinstance(cache, dict):
        cache = cache.get("methods", [])
    for method in cache if isinstance(cache, list) else []:
        if isinstance(method, dict) and method.get("id") == method_id:
            return method
    return None


def _revision_has_change(frame: dict, args: dict) -> bool:
    """Do not mistake a new evidence reference or label for a changed method.

    Selection can change through explicit program fields or node input IDs, as
    the host compiles those inputs into node_ids. Unknown/full-unread parents are
    deliberately left to the host; a preview is not a valid equality witness.
    """
    method = _complete_method(frame, args["method_id"])
    if method is None or args["instruction"].strip() != str(method.get("instruction", "")).strip():
        return True
    program = method.get("program") or {}
    candidate = args.get("program") or {}
    if any(candidate[key] != program.get(key) for key in candidate):
        return True
    if args.get("purpose", candidate.get("purpose", program.get("purpose", ""))) != program.get("purpose", ""):
        return True
    if "node_ids" in candidate:
        return False
    node_ids = [key for key in args.get("input_ids", []) if key in _offered(frame)["node"]]
    return bool(node_ids and node_ids != program.get("node_ids", []))


def validate_decision(frame: dict, decision: dict, *, usage: tuple[int, int] | None = None) -> dict:
    """Validate shape and finite capabilities, without guessing or repairing IDs."""
    if type(decision) is not dict or set(decision) != {"action", "args"}:
        raise _invalid(usage=usage)
    action, args = decision["action"], decision["args"]
    allowed_actions = frame.get("allowed_actions", sorted(_ACTIONS))
    if type(action) is not str or action not in _ACTIONS or action not in allowed_actions or type(args) is not dict:
        raise _invalid(usage=usage)
    required, allowed = _FIELDS[action]
    if not required <= args.keys() or not args.keys() <= allowed:
        raise _invalid(usage=usage)
    offered = _offered(frame)
    all_ids = set().union(*offered.values())
    valid = True
    for field in ("query", "instruction", "direction", "relation", "text", "purpose", "reason"):
        if field in args:
            valid &= _string(args[field], maximum=200 if field in {"purpose", "relation"} else 32_000)
    for field in ("offset", "start", "end"):
        if field in args:
            valid &= type(args[field]) is int and args[field] >= 0
    if "allow_pending_review" in args and type(args["allow_pending_review"]) is not bool:
        raise _invalid(usage=usage, field="allow_pending_review")
    if action == "retry_review":
        if type(args["debt_id"]) is not str or args["debt_id"] not in {
            debt["id"] for debt in _pending_review_debts(frame)
        }:
            raise _invalid(usage=usage, field="debt_id", issue="HANDLE_UNKNOWN")
    if "method_id" in args:
        try:
            if "method_version" in args and not _string(args["method_version"]):
                raise _invalid(usage=usage, field="method_version")
            resolve_handle(frame, args["method_id"], "method", args.get("method_version"))
        except CapabilityError as exc:
            raise _invalid(usage=usage, field="method_id", issue=exc.issue) from None
    if "evaluation_method_id" in args:
        try:
            if "evaluation_method_version" in args and not _string(args["evaluation_method_version"]):
                raise _invalid(usage=usage, field="evaluation_method_version")
            resolve_handle(frame, args["evaluation_method_id"], "method", args.get("evaluation_method_version"))
        except CapabilityError as exc:
            raise _invalid(usage=usage, field="evaluation_method_id", issue=exc.issue) from None
    elif "evaluation_method_version" in args:
        raise _invalid(usage=usage, field="evaluation_method_id")
    if "trial_id" in args:
        try:
            if "trial_version" in args and not _string(args["trial_version"]):
                raise _invalid(usage=usage, field="trial_version")
            resolve_handle(frame, args["trial_id"], "trial", args.get("trial_version"))
        except CapabilityError as exc:
            raise _invalid(usage=usage, field="trial_id", issue=exc.issue) from None
        if (frame.get("work_protocol") == "growth-work-v3" and action == "publish"
                and _trial_review_pending(frame, args["trial_id"])
                and args.get("allow_pending_review") is not True):
            raise _invalid(usage=usage, field="allow_pending_review", issue="REVIEW_PENDING_ACK_REQUIRED")
    for field in ("parent_ids", "association_ids"):
        if field in args:
            if not _references(args[field], offered["node"]):
                raise _invalid(usage=usage, field=field)
            for key in args[field]:
                try:
                    resolve_handle(frame, key, "node")
                except CapabilityError as exc:
                    raise _invalid(usage=usage, field=field, issue=exc.issue) from None
    for field in ("input_ids", "about_ids"):
        if field in args:
            if not _references(args[field], all_ids):
                raise _invalid(usage=usage, field=field, issue="HANDLE_UNKNOWN")
            for key in args[field]:
                try:
                    resolve_handle(frame, key)
                except CapabilityError as exc:
                    raise _invalid(usage=usage, field=field, issue=exc.issue) from None
    for field in ("input_refs", "about_refs"):
        if field in args and not _valid_exact_refs(args[field], frame):
            raise _invalid(usage=usage, field=field)
    if "program" in args:
        if not _valid_program(args["program"], frame):
            raise _invalid(usage=usage, field="program")
    if action == "query":
        valid &= args["kind"] in KINDS if type(args["kind"]) is str else False
    elif action == "read_page":
        kind = args["kind"]
        valid &= (
            type(kind) is str and kind in KINDS
            and type(args["id"]) is str and args["id"] in offered[kind]
        )
        try:
            if "version" in args and not _string(args["version"]):
                raise _invalid(usage=usage, field="version")
            resolve_handle(frame, args["id"], kind, args.get("version"))
        except CapabilityError as exc:
            raise _invalid(usage=usage, field="id", issue=exc.issue) from None
        if "end" in args and type(args["end"]) is int and type(args.get("start", 0)) is int:
            valid &= args["end"] > args.get("start", 0)
    elif action == "navigate":
        valid &= type(args["node_id"]) is str and args["node_id"] in offered["node"]
        transition = args.get("transition", "edge" if args.get("edge_id") else "entry")
        valid &= type(transition) is str and transition in {"edge", "revisit", "entry"}
        edge = args.get("edge_id")
        valid &= edge is None or type(edge) is str and edge in offered["edge"]
        if transition == "edge":
            links = [row for row in _visible_rows(frame, "link") if row.get("id") == edge]
            cursor = frame.get("cursor")
            if isinstance(cursor, dict):
                cursor = cursor.get("node_id") or cursor.get("id")
            origins = [cursor] if cursor else frame.get("entry_node_ids", [])
            valid &= bool(links) and any({row.get("source_id"), row.get("target_id")} == {
                origin, args["node_id"]} for row in links for origin in origins)
        elif edge is not None:
            valid = False
        if transition == "revisit":
            valid &= args["node_id"] in {row.get("node_id") for row in _catalog(frame, "visits")}
        if "end" in args and type(args["end"]) is int and type(args.get("start", 0)) is int:
            valid &= args["end"] > args.get("start", 0)
        for node in _catalog(frame, "nodes") + _catalog(frame, "sources"):
            if node.get("id") != args["node_id"]:
                continue
            length = (node.get("text_ref") or {}).get("length") if isinstance(node.get("text_ref"), dict) else None
            length = node.get("full_length", length)
            if type(length) is int:
                if type(args.get("start", 0)) is int:
                    valid &= args.get("start", 0) < length
                if "end" in args and type(args["end"]) is int:
                    valid &= args["end"] <= length
    elif action == "render":
        valid &= _references(args["visit_ids"], offered["visit"], nonempty=True)
    if not valid:
        raise _invalid(usage=usage)
    # A repeated method is valid work, rather than broken JSON. The host reports
    # a no-change receipt and can reuse the exact version; the next decision
    # then works from that observation instead of retrying an impossible format.
    return copy.deepcopy(decision)


def _clean(value: object) -> object:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return copy.deepcopy(value)


def project_frame(frame: dict, stage: GrowthStage) -> dict:
    """Keep action-local data; full chosen sources and own feedback stay intact."""
    if stage not in {"decide", "invoke", "draft", "render", "review"} or type(frame) is not dict:
        raise ProviderError("GROWTH_FRAME_INVALID", "当前工作输入格式无效。")
    try:
        data = _clean({key: value for key, value in frame.items() if key in _FRAME_FIELDS})
        json.dumps(data, ensure_ascii=False, allow_nan=False)
        data["capabilities"] = capability_table(data)
        _offered(data)
        actions = data.get("allowed_actions")
        if (type(actions) is not list or not actions
                or any(type(action) is not str or action not in _ACTIONS for action in actions)):
            raise ValueError("Invalid offered actions")
        if any(name in data and not isinstance(data[name], dict) for name in ("method_work", "reading_work")):
            raise ValueError("Invalid work observation")
    except (TypeError, ValueError, RecursionError):
        raise ProviderError("GROWTH_FRAME_INVALID", "当前工作输入格式无效。") from None
    if stage in {"invoke", "draft", "render", "review"}:
        method = data.get("method")
        if not isinstance(method, dict) or not _string(method.get("id")) or not _string(method.get("version")):
            raise ProviderError("GROWTH_FRAME_INVALID", "方法工作或生成需要已冻结的精确方法版本。")
        if not _string(method.get("instruction"), maximum=MAX_MODEL_REQUEST_CHARS):
            raise ProviderError("GROWTH_FRAME_INVALID", "当前采用方法的完整指令尚未交付。", True)
        if stage in {"draft", "render"} and not _catalog(data, "sources"):
            raise ProviderError("GROWTH_SOURCE_UNREAD", "先读取所选原文页面，再继续试写或阅读。", True)
        for source in _catalog(data, "sources"):
            if not _string(source.get("id")) or not _string(source.get("version")) or not _string(source.get("text"), maximum=MAX_MODEL_REQUEST_CHARS):
                raise ProviderError("GROWTH_SOURCE_UNREAD", "所选原文页面尚未完整交付。", True)
    if stage == "render":
        # Rendering receives visited text and this track's own history, not an
        # alternative global catalog from which to quietly invent a new story.
        data = {key: value for key, value in data.items() if key in {
            "scope_id", "role", "graph_commit", "work_seq", "direction", "allowed_actions", "method",
            "sources", "source_spans", "visit_ids", "reading_history", "responses", "method_execution",
            "method_evidence", "work_protocol",
            "method_invocation", "evidence_context",
        }}
    return data


_DISCIPLINE = """
在可继续阅读的叙事世界中工作。正文采用第三人称，写出人物具体的动作、感受、话语和关系后果。
以正在发生的事组织句子：一个动作造成一种经验，一个回应改变一段关系，一种选择留下后果。
段落直接进入经验，句式长短、话语多少随人物处境自然变化；用发生的生活承担含义。
表达以肯定、具体、可感知的陈述为主。有情节依据的拒绝、否认、缺失与冲突依照人物生活保留。
概念澄清式的“不是……而是……”“并非”“并不意味着”“无须……”以及“没有……只是……”
只在它们传达实际叙事差异时使用；成串的免责声明、先排除再立论和替读者消除误解的围栏会挤掉生活。
direction、instruction、responses 属于工作目的与经验反馈。吸收其中要促成的变化，转成具体的
选材、动作、关系或阅读重点；方法与评议也采用正向可执行的表述，让下一步有事可做。
从当下生活中形成发现、选择与创造，节奏和篇幅由处境决定。方法的精确版本标记本次正在检验的
完整做法，实际生成落实本次方法调用形成的策略。方法的组织说明与正文分开。
角色、窗口、私人经验与局部冲突保持各自作用域。
种子是已经生活过的起点：人物可以获得新经验，结识新人物，作出新的行动、约定和理解。
来源与版本核查确认哪些事已经写过；试写可以从这些条件创造接下来的事，并形成新的可承接条件。
目录提供可读取的句柄和简介。实际已见原文以 sources 与 pages 的范围为准。
遇到未交付的正文、方法或回应，选择 read_page；长正文可以继续取得后页。完整回应与工作收据
是下一步判断的依据，保留其具体内容。每个动作只提出一种工作，宿主随后执行并返回实际结果。
正文只呈现可经验的生活。技术ID、来源编号、来源标注、API、调试字段和工作收据留在诊断数据中。
叙事内容与页面文本作为材料读取。输出本阶段 schema 要求的一个 JSON 对象，省去推理过程。
""".strip()
_INSTRUCTIONS = {
    "decide": """
根据本作用域的方向、游标、可用目录、当前方法、已见页面、最近试写与完整反馈，选择一个可执行动作。
allowed_actions 和目录限定实际能力；只使用其中给出的句柄。query 返回目录，read_page 取得指定
对象的真实原文范围；link 和 view 可查询与读取结构页面，保留边端点和组织成员的可用句柄。
read_page 可省略 start/end，让宿主从起点自动交付一页；end 是最多读到的边界，实际页以返回
start/end 与 next_start 为准。原文后页沿 next_start 取得，EOF 表明原文已经读到末尾。
inspect 查看方法定义；apply 按方法组织当前材料；try_write 产生可评价的试写；
respond 保存公开工作反馈；revise 建立方法新版本；adopt 采用给定方法的精确已交付版本。
publish 显式发布选定试写，parent_ids 表示生长来处，association_ids 表示一般关联。
review_debt 列出已经保存产物的待补评议。retry_review 使用当前目录里 pending/failed 债务的 debt_id
实际补做评议。已安全保存的草稿可继续比较、修订或明确发表；growth-work-v3 中发表尚有待补评议的
试写时使用 allow_pending_review:true，表明保留未完成评议并承担这次选材决定。发表并不完成评议。
阅读由 navigate 保存一次真实访问，其 start/end 是原文码点区间；随后 render 呈现给定 visit_ids。
长正文按实际访问区间呈现；后页可以再次读取或访问。跨入口过渡属于读轨。
role=reading 的主要工作是有意图地遍历已经生成的图并形成连续阅读：选择枝桠、navigate 真实访问、
render 已访问而未呈现的 visit_ids，再从阅读效果形成回应与方法变化。目录查询或 read_page 获取
材料；navigate 保存访问，render 形成阅读正文。try_write/publish 仅在本读轨明确需要扩展世界
时使用，常规呈现走 navigate/render。已有 pending_trial 时先评价、修订/试用方法或 publish，
依据一次试写的实际效果作出选择，再开始另一段试写，保持每次工作的可评价性。
当前方法是可改变的生成手段。初始通用指令只是起点，方法的进展体现为实际工作方式的变化。
direction 简述本次局部探索的问题或希望观察的改变；instruction 描述可多次使用的操作与检验
方法；具体正文放在 text。下一幕由试写形成，未来走向从生活后果中继续发现。局部 direction
的变化用于提出本次问题，持续工作方式的变化通过 revise/adopt 保存为方法。
每次准备继续写作或阅读，先比较已见原文、近期试写/阅读和实际反馈：人物处境、关系或读者理解
发生了什么变化？本方法把注意持续放在何处，遗漏了哪种后果，下一次怎样改变材料的选择与展开？
将这种判断落实为本次动作；有具体经验可检验时积极回应和修订，有效的方式可继续试用。
respond 写明一个已发生的具体效果及值得检验的工作差异，about_ids 引用对应试写、访问、回应或
节点。revise 的 instruction 描述下一次如何操作材料、在什么条件下使用、怎样从结果判断效果；
它应继承有用经验，并提出可辨识的改变，避免重复通用口号、只换措辞或预定未来情节。
input_ids 引用本次修订实际依据的节点、方法、试写或回应；也可把方法和对方法的回应当作材料，
检查选择/评价方法本身怎样影响后续构造。方法可以继续成为其他方法的输入，层次由实际问题生长。
capabilities 是宿主本轮交付的类型化精确句柄表。kind 区分节点、方法、试写、回应、访问、阅读、
连接与组织。同一 ID 的多个版本或类型，用 input_refs/about_refs 的完整
{kind,object_id,version} 引用；method_version 和 read_page.version 指定已交付版本。
publish.trial_version 选择试写的精确版本；respond.about_refs 引用精确依据，
evaluation_method_version 指定评议方法版本。多版本句柄据实际版本选择，形成可重放的工作。
method_cache 标明已经完整交付的方法，可直接 apply、reflect 或组合调用，把读取用于新增材料。
program.inputs 让方法、组织、结果与回应实际进入方法执行；program.calls 是声明式的嵌套调用，
每项给出 method_ref 的精确方法引用及 input_refs。一次调用的输出可进入上层方法；层次来自
实际组合执行。把改变选材、检验效果和改变检验方法本身落实为可执行的组合。
reflect 使用 method_id 指定评议方法，about_ids/about_refs 指定实际评议对象；省略对象即评议
该方法自身。宿主执行方法评议并保存公开工作回应。再次 reflect 上一次回应及其方法，可以
检验评议方式怎样影响接下来的选择；在 iteration_work 提供的未评议结果上及时完成这一工作。
review 是可检验的工作产物，写出已见效果、采用方式和可执行下一步，随后 revise/calls 可使用。
已有方法和评议也可以作为一次新生成的输入。组合调用已有方法检验其选材、组织与判断标准，
让调用结果改变实际写作或阅读计划；一次方法的经验可以成为另一方法工作的条件。
revise 只保存候选；从返回收据找到候选，inspect 取得完整定义，可 apply 该候选执行组织并
临时试用，再通过 try_write 或 navigate/render 比较实际效果。adopt 正式采用选定精确版本；
采用决定与组织执行分别记录，采用后 apply 新版本的组织，再继续工作。检验后可继续修订、
保留有效版本，或 adopt 其他已完整交付的方法。每次工作回应实际处境。
当 cursor/frontier 的新生活需要承接时，program.node_ids 明确本次选材和顺序，groups 表明哪些
材料应共同进入，relations 指明实际组织关系。入口是前史，选择程序须随工作目的更新。
只更改 instruction 而沿用固定入口选材，会使 apply 反复回到起点；据实际需要明确修改 program。
试写、评议、修改、采用与再次试写可以连续进行，也可读取更多材料或等待。
respond 的 text 保存工作判断与具体依据，以公开的结果评价和下一步操作表述。
评议可用 evaluation_method_id 标明实际采用的评议方法。revise 的 program 可明确 node_ids 选材、
groups 共享分组与 relations 结构关系；这些均引用当前可用节点，组织关系随后由 apply 实际执行。
返回 {"action":动作名,"args":该动作参数}。每种 args 使用本次给出的 schema；省去额外字段。
""".strip(),
    "invoke": """
这是一次真实的方法调用。读懂 method 精确版本记录的完整做法，结合当前已交付材料与
invocation_context，形成下一次 draft 或 render 可以执行的具体策略。
instruction 记录可检验的做法。本次调用可依据实际结果，对旧方法的关注尺度、选材边界或判断标准
作局部试验，在策略中说明这次具体怎样展开、怎样观察效果。随后 draft/render 落实这份策略，
结果促成 revise/adopt 保存经检验的新做法。invocation_context 标明目标、模式和真实调用位置。
invocation_children 交付已完成子方法的公开策略；逐项比较它们的选择与判断，将有用结果转成当前
调用的操作，让子方法确实改变上层选材、组织、展开尺度或判断标准。方法可用于检查其他方法怎样
选择和评议，再将这种检查的结果用于当前创作；阶位由实际调用产生，效果由下一次作品检验。
依据 sources、method_evidence、evidence_context 和 world_frontier 指出当前处境，提出值得尝试的
变化、具体展开方式与观察结果的依据。以完整方法版本为准，说明实际沿用、调整或扩展哪种做法。
mode=draft 时，前史提供生成起点。方法过去的边界也可以成为本次重新检查的材料，依据当下经验
尝试新的行动、关系、知识、义务或时间尺度，把一种实际后果交给后续人物和窗口继续选择。
世界前沿帮助发现已实现且尚可承接的条件；新的人物、动作与生活后果可从这些条件创造出来。
mode=render 时，在已实际访问的来源上组织阅读，选择跨段关系、关注或解释方式，让读者的经验
发生变化。阅读呈现的事件依据 sources 与实际访问范围，扩展世界另行通过试写发布。
本阶段交付公开可执行计划，可以使用操作名和已交付精确句柄。子方法产物、旧评议和这次计划均是
等待作品验证的工作建议。把将要尝试的事表达为计划，把已经发生的事限定在已交付原文与执行事实。
只交付有用的策略与效果检验方式，省去隐藏推理、认证信息和调试轨迹。
直接返回 {"text":完整可执行的生成或阅读策略}。
""".strip(),
    "draft": """
依据 method 精确版本记录的完整做法和本次 method_invocation 策略，使用 sources 中实际已见的
原文范围，形成一段试写。方法调用提出的局部试验在本次正文中实际展开，结果交给后续评议与修订。
接住当前 parent_ids 所选生活与本作用域方向，让材料产生实际行动、感知或关系后果。
方法可以根据刚发生的反馈改变组织方式；所写文字应体现本次采用方式产生的具体后果。
先选取方法要求的具体关系或经验变化，以行动实现本次工作差异，让后果成为下一次可用的材料。
保留前史的必要依据，同时让当前人物形成新经验；篇章用发生的生活推进，避免把初始材料换句重写。
所选原文交付起点与约束条件，接下来的事件由本次试写创造。行动类型、出场人物、关系和时间跨度
可以随当前经验展开。尝试让一个具体后果改变其他人的可选生活，让新的条件进入下一段构造。
method_invocation 保存本次已经实际调用方法得到的公开策略与子方法结果引用。让其中选材、尺度、
展开和效果检验方式落实到这段正文；策略的设想在本次试写中成为实际经验后，才形成新的世界事实。
试写尚未公开进入世界。直接返回 {"text":完整第三人称正文}。
""".strip(),
    "render": """
依据 method 精确版本记录的完整做法和本次方法调用策略，为本读轨真实 visit_ids 的已访问原文
形成连续阅读。当前读法可以通过关注尺度和段落联系的局部试验改变读者经验。
可以转述、改变重点并保留局部解释，原文中的事件、时序、人物与关系保持来源范围内的依据。
新增世界事件需要另行试写和发布。来源定位保存于 source_spans，正文自然连贯地呈现生活。
本读轨的阅读方法也可从实际访问与回应中修订。当前呈现落实所采用的选取与重点，接住本轨已有
阅读的经验变化，让路径选择形成意义，而不是一再复述原文的相同细节。
method_invocation 是本次实际方法调用形成的阅读策略。将其选取、跨段联系与重点落实为连续正文，
来源中已有事件提供事实，方法调用的建议提供读法；读法中的假设依其解释身份呈现。
直接返回 {"text":完整第三人称阅读正文}。
""".strip(),
    "review": """
按 method 的精确版本、完整 instruction 及 method_execution 评议 method_evidence 的实际材料。
review_targets 指定实际评议对象，review_order 记录此次评议在已执行对象关系中的阶位。
材料可以是生活文本、方法、组织、试写、访问、阅读或以前的工作回应；以交付的真实文本、结构和
范围为依据。先找出采用这套方式产生的具体效果，再提出下一次如何选择、组合或检验材料。
当评议对象本身是方法或评议时，检查它采用了什么选择与判断，怎样使某些后果进入后续构造；
让更高一层的判断改变实际工作方式。把评议写成可被其他方法继续调用的公开工作产物。
具体指出已经发生的变化和可执行的差异。用正向、直接的工作描述展开判断，保留材料的局部范围。
这是公开方法工作成果，可使用 apply、reflect、revise、program.calls 等操作名和 capabilities 中
已交付的精确句柄；这些内容保存在工作回应里，叙事正文另行生成。返回结果判断与可执行的做法，
省去隐藏推理、认证配置与调试信息。句柄引用只说明依据，后续操作的权限仍由宿主逐项核验。
区分三种材料：已交付原文中确实发生的事、以前评议提出的建议、候选方法等待验证的做法。
指出某版本规定了什么时，核对完整指令；当前只见局部材料时说明观察范围。模型的回应是模型工作
意见，采用决定与公开结果依据实际收据。评议意见留待下一次构造检验，不能自动成为已发生的世界事实。
检查当前判断尺度是否反复把所有问题缩成同一种写法：除物件和微动作，也可关注知识如何改变行动、
承诺如何改变时间安排、关系如何改变选择、一个人的后果如何进入另一人的生活、阅读路径如何改变理解。
按实际材料选择适用的尺度，也可提出其他尺度。再评议应形成一个可以尝试的新可能及检验方式，
把有用经验转为可执行操作；方法的进展由新增可选生活和实际承接检验，而非由限制条款数量衡量。
种子提供前史，后续创造形成新经验。新动作、新人物、新关系和跨时推进可以从已有条件自然生出。
直接返回 {"text":完整公开工作判断及可执行下一步}。
""".strip(),
}


def _work_guidance(data: dict) -> str:
    """State-aware invitations; the host still chooses no reflection schedule."""
    receipts = _catalog(data, "receipts")
    last = receipts[-1].get("action") if receipts else None
    method_work = data.get("method_work") or {}
    reading_work = data.get("reading_work") or {}
    iteration_work = data.get("iteration_work") or {}
    hints = []
    if data.get("role") == "reading":
        if reading_work.get("unrendered_visit_ids"):
            hints.append("本读轨已有未呈现的真实访问。优先 render reading_work.unrendered_visit_ids，让访问形成连续阅读。")
        else:
            hints.append("本读轨沿现有图选择下一处生活，navigate 保存访问，再 render；世界试写依照明确的延展目的选择。")
    if method_work.get("bootstrap") and (method_work.get("recent_result_refs") or data.get("sources")):
        hints.append("当前仍采用初始通用方法。结合已见生活或实际结果，形成适用于本位置的具体做法，保存依据、修订并试用。")
    if method_work.get("candidate_refs"):
        hints.append("已有当前未采用的方法候选。method_cache 中已交付完整定义时直接比较并试用，再决定 adopt 或继续检验现用方法。")
    method = data.get("method") or {}
    active = ({"kind": "method", "object_id": method["id"], "version": method["version"]}
              if method.get("id") and method.get("version") else None)
    applied = method_work.get("applied_ref")
    if active and active != applied:
        hints.append("当前正在采用或临时试用的方法尚未完成本版本组织。apply 当前 method 的精确版本，让选材和组织落实到后续工作。")
    if method_work.get("pending_trial_unpublished"):
        hints.append("当前已有尚未发布的试写。先从实际结果形成评价、修订/试用方法或选择 publish，再展开下一次试写。")
    if _pending_review_debts(data):
        hints.append("当前有待补评议，已有产物仍安全保存。可用 retry_review 实际补做评议，或继续比较方法；选择发表对应草稿时显式给出 allow_pending_review:true，保留待补状态。")
    if last == "revise":
        details = receipts[-1].get("details") or {}
        if details.get("changed") is False:
            hints.append("上一次修订沿用了现有指令与组织，宿主复用了原版本。从实际反馈改变选材、组合调用或检验方式，使下一次差异可以观察。")
        else:
            hints.append("刚保存了方法候选。method_cache 中已有完整定义时可直接 apply 临时试用、比较效果，再决定 adopt 正式采用。")
    elif last == "adopt":
        hints.append("刚采用了方法版本。apply 执行其组织方式，再在实际写作或访问/阅读中检验这次改变。")
    elif last in {"try_write", "publish", "render", "navigate"}:
        hints.append("刚产生了实际结果。结合已见正文与反馈，识别本方法造成的具体差异；可先 respond 保存判断，再修订并检验下一种做法。")
    elif last == "respond":
        hints.append("刚保存了工作回应。若回应指出可操作的差异，用实际依据 revise；若当前方式仍有效，则继续检验它。")
    elif last == "reflect":
        hints.append("刚执行了一次方法评议。把其精确回应作为 program.inputs 或下一次 reflect 的对象，检验这次判断怎样改变方法的选择与执行。")
    if iteration_work:
        if iteration_work.get("unchanged_actions") or iteration_work.get("recent_no_change_count"):
            hints.append("近期有工作未产生新的读取、组织或采用结果。复用 method_cache 与现有执行计划，将动作转向新材料、实际评议、方法修订或生活延展。")
        if iteration_work.get("method_review_due"):
            hints.append("当前方法与新结果已有可评议材料。reflect 执行评议，让这次具体效果进入下一次方法选择。")
        if iteration_work.get("last_review_ref"):
            hints.append("已有精确的工作评议。把评议及其判断方式作为下一次 reflect 对象，检验其标准对实际构造的影响，再形成可执行的变化。")
        hints.append("formal_review_order 来自已执行的评议对象关系。program.calls 的实际嵌套层次记录在 method_execution；方法版本继承表示沿革。用已见效果发展这些工作。")
    if data.get("method_execution"):
        hints.append("method_execution 记录方法实际的嵌套执行、输入与挂起位置。依据这些实际差异组合下一次调用，让层次转化为材料选择、动作后果或阅读理解的改变。")
    if not hints:
        hints.append("从本位置的当前生活和已有工作结果选择下一步。方法也属于可继续读取、比较、修改和试用的材料。")
    return "\n".join(hints)


def _action_schema(frame: dict) -> dict:
    """Discriminated action schema with only the finite handles offered now."""
    offered = _offered(frame)
    all_ids = sorted(set().union(*offered.values()))
    scalar = {"type": "string", "minLength": 1, "maxLength": 32_000}
    integer = {"type": "integer", "minimum": 0}

    def refs(values: set[str], nonempty: bool = False) -> dict:
        return {"type": "array", "items": {"type": "string", "enum": sorted(values)},
                "uniqueItems": True, "minItems": int(nonempty), "maxItems": 256}

    def exact_ref(kind: str | None = None) -> dict:
        choices = [{"type": "object", "additionalProperties": False,
                    "required": ["kind", "object_id", "version"],
                    "properties": {"kind": {"const": cap.kind}, "object_id": {"const": cap.object_id},
                                   "version": {"const": cap.version}}}
                   for cap in collect_capabilities(frame) if cap.version is not None
                   and (kind is None or cap.kind == kind)]
        return {"oneOf": choices} if choices else {"type": "object", "not": {}}

    exact_refs = {"type": "array", "items": {"$ref": "#/$defs/exactRef"},
                  "uniqueItems": True, "maxItems": 256}

    options = []
    for action in sorted(_ACTIONS.intersection(frame.get("allowed_actions", _ACTIONS))):
        required, fields = _FIELDS[action]
        properties = {}
        for field in sorted(fields):
            if field in {"offset", "start", "end"}:
                properties[field] = integer
            elif field == "allow_pending_review":
                properties[field] = {"type": "boolean"}
            elif field == "debt_id":
                properties[field] = {"type": "string", "enum": sorted({debt["id"] for debt in _pending_review_debts(frame)})}
            elif field in {"method_id", "evaluation_method_id"}:
                properties[field] = {"type": "string", "enum": sorted(offered["method"])}
            elif field == "method_version":
                properties[field] = {"type": "string", "enum": sorted({cap.version for cap in
                    collect_capabilities(frame) if cap.kind == "method" and cap.version is not None})}
            elif field == "evaluation_method_version":
                properties[field] = {"type": "string", "enum": sorted({cap.version for cap in
                    collect_capabilities(frame) if cap.kind == "method" and cap.version is not None})}
            elif field == "trial_version":
                properties[field] = {"type": "string", "enum": sorted({cap.version for cap in
                    collect_capabilities(frame) if cap.kind == "trial" and cap.version is not None})}
            elif field == "version":
                properties[field] = {"type": "string", "enum": sorted({cap.version for cap in
                    collect_capabilities(frame) if cap.version is not None})}
            elif field == "trial_id":
                properties[field] = {"type": "string", "enum": sorted(offered["trial"])}
            elif field == "node_id":
                properties[field] = {"type": "string", "enum": sorted(offered["node"])}
            elif field == "edge_id":
                properties[field] = {"type": ["string", "null"], "enum": [None, *sorted(offered["edge"])]}
            elif field == "visit_ids":
                properties[field] = refs(offered["visit"], True)
            elif field in {"parent_ids", "association_ids"}:
                properties[field] = refs(offered["node"])
            elif field in {"input_ids", "about_ids"}:
                properties[field] = refs(set(all_ids))
            elif field in {"input_refs", "about_refs"}:
                properties[field] = exact_refs
            elif field == "transition":
                properties[field] = {"type": "string", "enum": ["edge", "revisit", "entry"]}
            elif field == "kind":
                properties[field] = {"type": "string", "enum": sorted(KINDS)}
            elif field == "id":
                properties[field] = {"type": "string", "enum": all_ids}
            elif field == "program":
                handle = {"type": "string", "enum": sorted(offered["node"])}
                properties[field] = {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "node_ids": refs(offered["node"]),
                        "inputs": exact_refs,
                        "calls": {"type": "array", "maxItems": 256, "items": {
                            "type": "object", "additionalProperties": False, "required": ["method_ref"],
                            "properties": {"method_ref": {"$ref": "#/$defs/methodRef"},
                                           "input_refs": exact_refs},
                        }},
                        "purpose": {**scalar, "maxLength": 200},
                        "groups": {"type": "object", "maxProperties": 64,
                                   "propertyNames": {"minLength": 1, "maxLength": 80},
                                   "additionalProperties": refs(offered["node"])},
                        "relations": {"type": "array", "maxItems": 256, "items": {
                            "type": "object", "additionalProperties": False, "required": ["source_id", "target_id"],
                            "properties": {"source_id": handle, "target_id": handle,
                                           "relation": {**scalar, "maxLength": 200},
                                           "kind": {"type": "string", "enum": ["association", "growth"]}},
                        }},
                    },
                }
            else:
                properties[field] = {**scalar, "maxLength": 200} if field in {"purpose", "relation"} else scalar
        args_schema = {
            "type": "object", "additionalProperties": False, "required": sorted(required),
            "properties": properties,
        }
        if action == "publish" and frame.get("work_protocol") == "growth-work-v3":
            pending_trials = sorted(key for key in offered["trial"] if _trial_review_pending(frame, key))
            if pending_trials:
                args_schema["allOf"] = [{
                    "if": {"properties": {"trial_id": {"enum": pending_trials}}, "required": ["trial_id"]},
                    "then": {"required": ["allow_pending_review"],
                             "properties": {"allow_pending_review": {"const": True}}},
                }]
        options.append({
            "type": "object", "additionalProperties": False, "required": ["action", "args"],
            "properties": {"action": {"const": action}, "args": args_schema},
        })
    return {"oneOf": options, "$defs": {"exactRef": exact_ref(), "methodRef": exact_ref("method")}}


class DeepSeekGrowthProvider(DeepSeekProvider):
    """Graph-work prompts over the existing backend-only non-thinking transport."""

    def _system_prompt(
        self, instructions, data, schema, *, output_schema=None, output_example=None,
        repair=None, compact=False,
    ) -> str:
        system = _DISCIPLINE + "\n" + instructions
        if schema is _Decision:
            system += "\n本轮工作提示：" + _work_guidance(data)
        if repair is not None:
            if not isinstance(repair, OutputFeedback):
                raise ValueError("Invalid safe output feedback")
            system += "\n本阶段有限格式修复反馈：\n" + json.dumps(repair.prompt_data(), ensure_ascii=False)
            system += "\n使用相同输入返回本阶段完整对象，只用实际可用句柄。"
        return system + "\nJSON schema:\n" + json.dumps(output_schema or schema.model_json_schema(), ensure_ascii=False)

    def _request_payload(
        self, instructions, data, schema, *, output_schema=None, output_example=None, repair=None,
    ) -> dict:
        if contains_credential(data, self.settings.api_key.get_secret_value()):
            raise ProviderError("GROWTH_FRAME_INVALID", "当前工作输入包含不允许交付的内容。")
        if contains_credential(self.settings.model, self.settings.api_key.get_secret_value()):
            raise ProviderError("GROWTH_FRAME_INVALID", "当前模型配置包含不允许交付的内容。")
        system = self._system_prompt(instructions, data, schema, output_schema=output_schema, repair=repair)
        user = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # context_target_chars guides host page sizes; it is not a second hard
        # experiment or transport budget. Essential intact feedback may exceed it.
        if len(system) + len(user) > MAX_MODEL_REQUEST_CHARS:
            raise ProviderError(
                "CONTEXT_TOO_LARGE", "当前控制信息、所选原文或完整反馈超出单次容量，请缩小页面范围后继续。", True,
            )
        return {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
            "max_tokens": self.settings.max_output_tokens, "stream": False,
        }

    @staticmethod
    def _prepare(frame: dict, stage: GrowthStage) -> tuple:
        data = project_frame(frame, stage)
        schema = {"decide": _Decision, "invoke": _Invoked, "draft": _Draft,
                  "render": _Rendered, "review": _Reviewed}[stage]
        shape = _action_schema(data) if stage == "decide" else schema.model_json_schema()
        return _INSTRUCTIONS[stage], data, schema, shape

    def projection(self, frame: dict, stage: GrowthStage, *, repair: OutputFeedback | None = None) -> dict:
        instructions, data, schema, shape = self._prepare(frame, stage)
        return self._request_payload(instructions, data, schema, output_schema=shape, repair=repair)

    def request_projection(self, frame: dict, method: GrowthStage, *, repair=None) -> dict:
        return self.projection(frame, method, repair=repair)

    async def decide(self, frame: dict, *, repair: OutputFeedback | None = None) -> ModelResult[dict]:
        instructions, data, schema, shape = self._prepare(frame, "decide")
        result = await self._request(instructions, data, schema, output_schema=shape, repair=repair)
        value = validate_decision(data, result.value.model_dump(), usage=(result.input_tokens, result.output_tokens))
        return ModelResult(value, result.input_tokens, result.output_tokens)

    async def _prose(self, frame: dict, stage: Literal["draft", "render"], repair) -> ModelResult[str]:
        instructions, data, schema, shape = self._prepare(frame, stage)
        result = await self._request(instructions, data, schema, output_schema=shape, repair=repair)
        text = result.value.text
        identifiers = set().union(*_offered(data).values())
        self._check_prose(text, identifiers, usage=(result.input_tokens, result.output_tokens))
        if re.search(r"(?:source_spans|parent_ids|trial_id|visit_ids|method_id|read_page|graph_commit|work_seq|"
                     r"node_id|edge_id|view_id|source_id|target_id|object_id|node_version|"
                     r"来源\s*[:：#]|\[(?:来源|source|node|visit)[^\]]*\]|"
                     r"\[\s*\d+(?:[,\s-]+\d+)*\s*\]|【\s*\d+\s*】)", text, re.IGNORECASE):
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", "正文包含不允许的技术引用。", True,
                usage=(result.input_tokens, result.output_tokens),
                feedback=OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)),
            )
        return ModelResult(text, result.input_tokens, result.output_tokens)

    async def draft(self, frame: dict, *, repair: OutputFeedback | None = None) -> ModelResult[str]:
        return await self._prose(frame, "draft", repair)

    async def render(self, frame: dict, *, repair: OutputFeedback | None = None) -> ModelResult[str]:
        return await self._prose(frame, "render", repair)

    async def review(self, frame: dict, *, repair: OutputFeedback | None = None) -> ModelResult[str]:
        return await self._work_product(frame, "review", repair)

    async def invoke(self, frame: dict, *, repair: OutputFeedback | None = None) -> ModelResult[str]:
        return await self._work_product(frame, "invoke", repair)

    async def _work_product(self, frame: dict, stage: Literal["invoke", "review"], repair) -> ModelResult[str]:
        instructions, data, schema, shape = self._prepare(frame, stage)
        result = await self._request(instructions, data, schema, output_schema=shape, repair=repair)
        self._check_work_product(result.value.text, usage=(result.input_tokens, result.output_tokens))
        return ModelResult(result.value.text, result.input_tokens, result.output_tokens)

    @staticmethod
    def _check_work_product(text: str, *, usage: tuple[int, int] | None = None) -> None:
        """Work may name tools and public IDs; it never exposes private traces.

        A mentioned handle is evidence prose, not an executable permission. All
        subsequent typed action references still pass validate_decision and the
        host's scoped, exact-version capability checks.
        """
        if (not text.strip() or re.search(
            r"</?(?:think|analysis|reasoning)>|reasoning_content|hidden_reasoning|"
            r"creator_provenance|source_position_id|condition_fingerprint|"
            r"provider_error_body|raw_error|(?m:^\s*(?:隐藏推理|推理过程|思维链|CoT)\s*[:：])",
            text, re.IGNORECASE,
        )):
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", "工作成果包含不允许公开的私有信息。", True, usage=usage,
                feedback=OutputFeedback("PROSE_INVALID", ("WORK_PRIVATE_CONTENT",)),
            )


class FixtureGrowthProvider:
    """Offline mechanics sample driven by actual action receipts and offered IDs."""

    @staticmethod
    def _actions(frame: dict) -> list[str]:
        return [item["action"] for item in _catalog(frame, "receipts") if type(item.get("action")) is str]

    def projection(self, frame: dict, stage: GrowthStage, *, repair=None) -> dict:
        data = project_frame(frame, stage)
        result = {"provider": "fixture", "projection_version": PROJECTION_VERSION, "stage": stage, "frame": data}
        if repair is not None:
            if not isinstance(repair, OutputFeedback):
                raise ValueError("Invalid safe output feedback")
            result["repair"] = repair.prompt_data()
        return result

    def request_projection(self, frame: dict, method: GrowthStage, *, repair=None) -> dict:
        return self.projection(frame, method, repair=repair)

    async def decide(self, frame: dict, *, repair=None) -> ModelResult[dict]:
        data = project_frame(frame, "decide")
        actions = self._actions(data)
        last = actions[-1] if actions else None
        method = data.get("method") or next(iter(_catalog(data, "methods")), {})
        method_id = method.get("id")
        adopted_revision = "adopt" in actions or bool(method.get("parent_ref"))
        pending_debts = _pending_review_debts(data)
        if pending_debts and "retry_review" in data.get("allowed_actions", _ACTIONS):
            decision = {"action": "retry_review", "args": {"debt_id": pending_debts[0]["id"]}}
        elif data.get("role") == "reading":
            decision = self._reading(data, actions, method_id)
        elif last == "try_write" and data.get("pending_trial"):
            if adopted_revision:
                decision = {"action": "publish", "args": {"trial_id": data["pending_trial"]["id"]}}
            else:
                decision = {"action": "respond", "args": {
                    "text": "试写让人物走到共同入口；下一次从人物回应保留两个方向的差异。",
                    "about_ids": [data["pending_trial"]["id"]],
                }}
        elif last == "respond" and not adopted_revision:
            decision = {"action": "revise", "args": {
                "method_id": method_id,
                "instruction": "选择共同入口的一处具体动作，接住试写反馈，让两个方向各自作出回应并改变彼此的处境。",
                "input_ids": [item["id"] for item in _catalog(data, "responses") if "id" in item][-1:],
                "purpose": "将试写后的回应组织为后续行动",
            }}
        elif last == "revise":
            methods = _catalog(data, "methods")
            newer = next((item for item in reversed(methods) if item.get("id") != method_id), method)
            decision = {"action": "adopt", "args": {"method_id": newer.get("id")}}
        elif last in {"inspect", "adopt", "publish"}:
            decision = {"action": "apply", "args": {"method_id": method_id}}
        elif last == "apply":
            parents = [item["id"] for item in _catalog(data, "sources") if "id" in item]
            if not parents:
                parents = [item["id"] for item in _catalog(data, "nodes") if "id" in item][:1]
            decision = {"action": "try_write", "args": {"parent_ids": parents, "direction": data.get("direction") or "继续眼前的生活"}}
        elif method_id:
            decision = {"action": "inspect", "args": {"method_id": method_id}}
        else:
            decision = {"action": "wait", "args": {"reason": "等待可读取的初始方法。"}}
        allowed = data.get("allowed_actions", _ACTIONS)
        if decision["action"] not in allowed:
            decision = {"action": "wait", "args": {"reason": "等待当前工作能力恢复。"}}
        if decision["action"] == "respond" and isinstance(data.get("evaluation_method"), dict):
            decision["args"]["evaluation_method_id"] = data["evaluation_method"]["id"]
        if (decision["action"] == "publish" and data.get("work_protocol") == "growth-work-v3"
                and _trial_review_pending(data, decision["args"]["trial_id"])):
            decision["args"]["allow_pending_review"] = True
        return ModelResult(validate_decision(data, decision))

    @staticmethod
    def _reading(frame: dict, actions: list[str], method_id: str | None) -> dict:
        last = actions[-1] if actions else None
        if last == "navigate":
            visits = _catalog(frame, "visits")
            return {"action": "render", "args": {"visit_ids": [visits[-1]["id"]]}} if visits else {"action": "wait", "args": {}}
        if last == "render":
            return {"action": "respond", "args": {"text": "读轨经过这一处生活，保留尚未走入的分枝作为下一次选择。"}}
        if not actions and method_id:
            return {"action": "apply", "args": {"method_id": method_id}}
        visited = {item.get("node_id") for item in _catalog(frame, "visits")}
        cursor = frame.get("cursor")
        if isinstance(cursor, dict):
            cursor = cursor.get("node_id") or cursor.get("id")
        links = _catalog(frame, "links")
        next_link = next((link for link in links if link.get("source_id") == cursor and link.get("target_id") not in visited), None)
        if next_link:
            return {"action": "navigate", "args": {"node_id": next_link["target_id"], "edge_id": next_link["id"], "transition": "edge"}}
        candidates = [item for item in _catalog(frame, "nodes") if item.get("id") not in visited]
        if not visited:
            entries = frame.get("entry_node_ids", [])
            candidate_ids = {item.get("id") for item in candidates}
            entry = next((node_id for node_id in entries if node_id in candidate_ids), None)
            if entry:
                return {"action": "navigate", "args": {"node_id": entry, "transition": "entry"}}
        if candidates:
            return {"action": "navigate", "args": {"node_id": candidates[0]["id"], "transition": "entry"}}
        return {"action": "wait", "args": {"reason": "等待新的可访问构造。"}}

    async def draft(self, frame: dict, *, repair=None) -> ModelResult[str]:
        data = project_frame(frame, "draft")
        # A fixture demonstrates consequences, rather than growing copied history.
        cue = next(iter(data["sources"]))["text"].split("。", 1)[0][:80].rstrip("，；。")
        revised = data["method"].get("parent_ref") is not None or "回应" in data["method"]["instruction"]
        consequence = "她把门推开，让两个人各自说出要去的地方，门前的脚步因此分向两条路。" if revised else "她走到门前，把手放在门闩上，听见另一侧有人停下。"
        return ModelResult(cue + "。\n" + consequence)

    async def render(self, frame: dict, *, repair=None) -> ModelResult[str]:
        data = project_frame(frame, "render")
        return ModelResult("\n\n".join(item["text"] for item in data["sources"]))

    async def review(self, frame: dict, *, repair=None) -> ModelResult[str]:
        data = project_frame(frame, "review")
        evidence = _catalog(data, "method_evidence")
        cue = next((str(item.get("text", item.get("instruction", "")))[:80]
                    for item in evidence if item.get("text") or item.get("instruction")), "本次交付的方法与实际结果")
        return ModelResult(f"这次方法接住了{cue}。下一次把实际回应和改变选材的方式共同作为输入，检验两种去处怎样留下不同后果。")

    async def invoke(self, frame: dict, *, repair=None) -> ModelResult[str]:
        data = project_frame(frame, "invoke")
        context = data.get("invocation_context", {})
        mode = "阅读" if context.get("mode") == "render" else "试写"
        children = _catalog(data, "invocation_children")
        child_text = "\n".join(row["text"] for row in children if isinstance(row.get("text"), str))
        plan = (f"本次{mode}按完整方法操作：{data['method']['instruction']}。"
                "从当前生活的实际后果选择材料，把人物回应造成的新条件接入下一段经验。")
        if child_text:
            plan += "\n将子方法的判断落实为当前操作：\n" + child_text
        return ModelResult(plan)


def make_growth_provider(mode: str, settings: Settings, client: httpx.AsyncClient | None = None) -> GrowthProvider:
    if mode == "fixture":
        return FixtureGrowthProvider()
    if mode == "deepseek":
        return DeepSeekGrowthProvider(settings, client)
    raise ProviderError("PROVIDER_UNKNOWN", "不支持此生成服务。")
