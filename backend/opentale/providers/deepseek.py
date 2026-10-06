"""Bounded, non-thinking DeepSeek JSON evaluator with no secret-bearing diagnostics."""

from __future__ import annotations

import json
import math
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from opentale.capacity import (
    MAX_FRAGMENT_TARGETS,
    MAX_MODEL_CONTENT_CHARS,
    MAX_MODEL_REQUEST_CHARS,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_OPERATION_TEXT_CHARS,
    MAX_PRESENTATION_TEXT_CHARS,
    MAX_PROPOSAL_FRAGMENTS,
    MAX_PROPOSAL_OPERATIONS,
    MAX_PROPOSAL_TEXT_CHARS,
    MAX_REALIZATION_TEXT_CHARS,
)
from opentale.config import Settings
from opentale.domain import GenerationFrame, Operation, Presentation, Proposal, ReadingFrame
from opentale.security import contains_credential as _contains_credential

from .base import ModelResult, ProviderError
from .feedback import OutputFeedback, schema_feedback
from .prose import NARRATIVE_DISCIPLINE
from .reading import (
    compile_reading_references,
    history_table,
    reading_table,
    required_focus,
    shared_expression_ids,
    validate_presentation,
)
from .reflection import (
    REFLECTION_BOUNDARY,
    REFLECTION_INSTRUCTIONS,
    WireReflection,
    compile_reflection,
    materials_data,
    reflection_data,
    reflection_schema,
)

T = TypeVar("T", bound=BaseModel)
# Compatibility aliases for callers inspecting this provider's transport bounds.
MAX_RESPONSE_BYTES = MAX_MODEL_RESPONSE_BYTES
MAX_CONTENT_CHARS = MAX_MODEL_CONTENT_CHARS
MAX_REQUEST_CHARS = MAX_MODEL_REQUEST_CHARS


class _Surface(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_REALIZATION_TEXT_CHARS)


class _FragmentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    interface_index: int
    role: str = Field(min_length=1, max_length=80)


class _NamedFragmentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_position_id: str
    expression_id: str
    role: str = Field(min_length=1, max_length=80)


class _Fragment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PROPOSAL_TEXT_CHARS)
    targets: list[_FragmentTarget] = Field(max_length=MAX_FRAGMENT_TARGETS)


class _FragmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fragments: list[_Fragment] = Field(min_length=1, max_length=MAX_PROPOSAL_FRAGMENTS)


class _ReflexiveFragmentProposal(_FragmentProposal):
    reflection: WireReflection | None = None


class _ClassicPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)
    focus_position_id: str
    expression_ids: list[str] = Field(min_length=1)
    reinterprets: list[str] = Field(default_factory=list)


class _ReflexivePresentation(_ClassicPresentation):
    expression_ids: list[str]
    reflection: WireReflection | None = None


class _IndexedPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PRESENTATION_TEXT_CHARS)
    focus_index: int
    expression_indices: list[int] = Field(min_length=1)
    reinterprets_indices: list[int] = Field(default_factory=list)


class _IndexedReflexivePresentation(_IndexedPresentation):
    expression_indices: list[int]
    reflection: WireReflection | None = None


class _PresentationResponse(RootModel[_IndexedPresentation | _ClassicPresentation]):
    """Accept exact historical references; new requests use local handles."""


class _ReflexivePresentationResponse(RootModel[_IndexedReflexivePresentation | _ReflexivePresentation]):
    """Reflexive permissions stay independent of the reading-reference format."""


class _NamedFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_PROPOSAL_TEXT_CHARS)
    targets: list[_NamedFragmentTarget] = Field(max_length=MAX_FRAGMENT_TARGETS)


class _NamedFragmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fragments: list[_NamedFragment] = Field(min_length=1, max_length=MAX_PROPOSAL_FRAGMENTS)


class _ProposalResponse(RootModel[_FragmentProposal | _NamedFragmentProposal | Proposal]):
    """Keep valid historical replies; new requests select program-owned interfaces."""


class _JSONSyntaxError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _local_route_expressions(frame: GenerationFrame) -> set[str]:
    if frame.reflection is not None:
        # Methods may select a surface or frozen choice without a writable
        # Expression carrier. Its material ID is never a new rebind permission.
        return {material.expression_id for material in frame.reflection.materials if material.expression_id}
    return {use["expression_id"] for use in frame.active_uses}


def _routing_table(frame: GenerationFrame) -> list[dict]:
    """Enumerate the exact old writable pairs; order is local to this request only.

    Index selection replaces copying two unrelated identifiers. It grants no new
    scopes and never uses a name to infer an identity or disclose private values.
    """
    expressions = _local_route_expressions(frame)
    routes = []
    for peer in sorted(frame.peers, key=lambda item: item["id"]):
        if peer["id"] == frame.position_id:
            continue
        allowed = expressions | {
            interface["expression_id"]
            for interface in frame.peer_interfaces
            if interface["position_id"] == peer["id"]
        }
        for expression_id in sorted(allowed):
            roles = sorted({
                interface["role"] for interface in frame.peer_interfaces
                if interface["position_id"] == peer["id"] and interface["expression_id"] == expression_id
            })
            routes.append({
                "interface_index": len(routes),
                "target_position_id": peer["id"],
                "target_name": peer["name"],
                "expression_id": expression_id,
                "current_roles": roles,
            })
    return routes


def _proposal_schema(frame: GenerationFrame) -> dict:
    """Publish only request-owned route handles and open narrative roles."""
    routes = _routing_table(frame)
    if not routes and frame.reflection is None:
        raise ProviderError("GENERATION_FRAME_INVALID", "当前局部没有可承接的同伴接口。")
    schema = (_ReflexiveFragmentProposal if frame.reflection else _FragmentProposal).model_json_schema()
    schema["$defs"]["_FragmentTarget"]["properties"]["interface_index"]["enum"] = [
        route["interface_index"] for route in routes
    ]
    schema["$defs"]["_Fragment"]["allOf"] = [{
        "if": {"properties": {"targets": {"minItems": 1}}},
        "then": {"properties": {"text": {"maxLength": MAX_OPERATION_TEXT_CHARS}}},
    }]
    if frame.reflection is not None:
        _with_reflection_schema(schema, frame.reflection)
    return schema


def _with_reflection_schema(schema: dict, frame) -> None:
    reflection = reflection_schema(frame)
    schema.setdefault("$defs", {}).update(reflection.pop("$defs", {}))
    schema["$defs"]["WireReflection"] = reflection


def _shared_interface(frame: GenerationFrame) -> dict | None:
    """Suggest an existing common scope; this never creates bindings or fixes proposals."""
    peers = {peer["id"] for peer in frame.peers} - {frame.position_id}
    choices = []
    for use in frame.active_uses:
        targets = sorted({
            interface["position_id"]
            for interface in frame.peer_interfaces
            if interface["position_id"] in peers
            and interface["expression_id"] == use["expression_id"]
            and interface["role"] == use["role"]
        })
        if len(targets) >= 2:
            choices.append((len(targets), use["expression_id"], use["role"], targets))
    if not choices:
        return None
    _, expression_id, role, targets = min(choices, key=lambda choice: (-choice[0], choice[1], choice[2]))
    return {"expression_id": expression_id, "role": role, "target_position_ids": targets}


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    # The transport byte cap bounds allocation. Check the content character cap only
    # after the whole envelope (including strict usage counts) has been validated.
    content: str = Field(min_length=1)


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    message: _Message
    finish_reason: str


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: _Usage


def _strict_json(raw: str | bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise _JSONSyntaxError("DUPLICATE_FIELD")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise _JSONSyntaxError("NON_JSON_NUMBER")

    def floating(raw_number: str) -> float:
        value = float(raw_number)
        if not math.isfinite(value):
            raise _JSONSyntaxError("NON_JSON_NUMBER")
        return value

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)


def _output_json(content: str) -> object:
    """Remove whole syntactic wrappers only; preserve all decoded narrative characters."""
    raw = content.strip()
    if raw.startswith("\ufeff"):
        raw = raw[1:].lstrip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    parsed = _strict_json(raw)
    # An API may return an object encoded inside one or two extra JSON strings.
    # Never extract an object from surrounding commentary or concatenate responses.
    for _ in range(2):
        if not isinstance(parsed, str):
            return parsed
        parsed = _strict_json(parsed)
    if isinstance(parsed, str):
        raise _JSONSyntaxError("WRAPPING_TOO_DEEP")
    return parsed


def _validate_output(schema: type[T], parsed: object) -> T:
    if schema in {_PresentationResponse, _ReflexivePresentationResponse}:
        indexed = schema is _ReflexivePresentationResponse
        selected = _IndexedReflexivePresentation if indexed else _IndexedPresentation
        # Do not allow mixed reference formats or infer a missing reference.
        # The selected schema supplies finite field-level repair information.
        if isinstance(parsed, dict) and not {
            "focus_index", "expression_indices", "reinterprets_indices",
        }.intersection(parsed) and {
            "focus_position_id", "expression_ids", "reinterprets",
        }.intersection(parsed):
            selected = _ReflexivePresentation if indexed else _ClassicPresentation
        wire = selected.model_validate(parsed, strict=True)
        return schema.model_construct(root=wire)
    if schema is _ProposalResponse:
        # Diagnose the selected wire shape instead of flattening errors from a union.
        legacy_fields = {"position_id", "based_on_revision", "text", "operations"}
        selected = _FragmentProposal
        if isinstance(parsed, dict):
            if "fragments" not in parsed and legacy_fields <= parsed.keys():
                selected = Proposal
            elif isinstance(parsed.get("fragments"), list):
                targets = [
                    target for fragment in parsed["fragments"] if isinstance(fragment, dict)
                    and isinstance(fragment.get("targets"), list) for target in fragment["targets"]
                    if isinstance(target, dict)
                ]
                # Select one documented wire shape. Mixed references are rejected
                # rather than guessed, with errors from just the selected schema.
                if not any("interface_index" in target for target in targets) and any(
                    "target_position_id" in target or "expression_id" in target for target in targets
                ):
                    selected = _NamedFragmentProposal
        wire = selected.model_validate(parsed, strict=True)
        return schema.model_construct(root=wire)
    return schema.model_validate(parsed, strict=True)


def _format_message(feedback: OutputFeedback) -> str:
    details = "；".join((feedback.category, *feedback.issues[:3]))
    return f"模型输出不符合当前表达格式（{details}）。"


def _presentation_schema(frame: ReadingFrame) -> dict:
    schema = (
        _IndexedReflexivePresentation if frame.reflection else _IndexedPresentation
    ).model_json_schema()
    if frame.reflection is None:
        schema["properties"].pop("reflection", None)
    else:
        _with_reflection_schema(schema, frame.reflection)
    schema["required"] = ["text", "focus_index", "expression_indices", "reinterprets_indices"]
    schema["oneOf"] = [
        {
            "properties": {
                "focus_index": {"const": row["focus_index"]},
                "expression_indices": {
                    "items": {"type": "integer", "enum": [
                        expression["expression_index"] for expression in row["expressions"]
                    ]},
                    **({} if frame.reflection else {
                        "contains": {"enum": [
                            expression["expression_index"] for expression in row["expressions"]
                            if expression["shared"]
                        ]}, "minContains": 1,
                    }),
                },
            },
        }
        for row in reading_table(frame)
    ]
    history = [row["history_index"] for row in history_table(frame)]
    if history:
        schema["properties"]["reinterprets_indices"]["items"]["enum"] = history
    else:
        schema["properties"]["reinterprets_indices"]["maxItems"] = 0
    return schema


def _actor_frame(frame: GenerationFrame) -> dict[str, object]:
    """An explicit allowlist prevents new debugging fields from reaching an actor."""
    return {
        "position_id": frame.position_id,
        "name": frame.name,
        "revision": frame.revision,
        "seed_text": frame.seed_text,
        "active_uses": [
            {
                "id": use.get("id"),
                "expression_id": use.get("expression_id"),
                "text": use.get("text", ""),
                "role": use.get("role", ""),
                "values": use.get("values", []),
            }
            for use in frame.active_uses
        ],
        "recent_surface": frame.recent_surface[-3:],
        "peers": [{"id": peer["id"], "name": peer["name"]} for peer in frame.peers],
        "peer_interfaces": [
            {key: interface[key] for key in ("position_id", "expression_id", "role") if key in interface}
            for interface in frame.peer_interfaces
        ],
    }


_NARRATIVE_DISCIPLINE = NARRATIVE_DISCIPLINE

# A syntactic placeholder avoids teaching all three stages the same literary beat.
_PROSE_PLACEHOLDER = "本次生成的正文字符串"


class DeepSeekProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def _request(
        self, instructions: str, data: dict, schema: type[T], *, output_schema: dict | None = None,
        output_example: dict | None = None, repair: OutputFeedback | None = None,
    ) -> ModelResult[T]:
        key = self.settings.api_key.get_secret_value()
        if not key:
            raise ProviderError("API_KEY_MISSING", "请先在本地配置文件中填写测试密钥。")
        system = _NARRATIVE_DISCIPLINE + "\n" + instructions
        if output_example is not None:
            system += "\n本阶段完整输出形状示例（正文占位文字需替换；示例只说明字段、类型和可用编号）：\n"
            system += json.dumps(output_example, ensure_ascii=False)
        if repair is not None:
            if not isinstance(repair, OutputFeedback):
                raise ValueError("Invalid safe output feedback")
            system += "\n上次本阶段输出未通过结构校验，有限反馈如下：\n"
            system += json.dumps(repair.prompt_data(), ensure_ascii=False)
            system += (
                "\n使用相同的当前输入重新生成一个完整的本阶段对象，补齐必需字段和正确类型。"
                "不要解释错误、输出推理、复制校验反馈到正文或改用其他阶段的结构。"
                "严格遵守本阶段 schema；不要猜测不存在的目标或补写输入中没有的权限。"
            )
        if "reflection" in data or "materials" in data:
            system += "\n" + REFLECTION_BOUNDARY
        system += "\nJSON schema:\n" + json.dumps(output_schema or schema.model_json_schema(), ensure_ascii=False)
        user = json.dumps(data, ensure_ascii=False)
        if len(system) + len(user) > MAX_REQUEST_CHARS:
            raise ProviderError("CONTEXT_TOO_LARGE", "当前局部的输入过长，请缩小活跃内容。")
        payload = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
        }
        url = self.settings.base_url + "/chat/completions"
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        usage: tuple[int, int] | None = None
        try:
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=self.settings.timeout_seconds,
                follow_redirects=False,
            ) as response:
                self._check_status(response.status_code)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderError("MODEL_OUTPUT_TOO_LARGE", "服务商响应超出安全长度。", True)
                    raw.extend(chunk)
            try:
                envelope = _Envelope.model_validate(_strict_json(bytes(raw)), strict=True)
            except (ValueError, ValidationError, UnicodeError, RecursionError):
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "服务商返回了无效响应。", True) from None
            usage = (envelope.usage.prompt_tokens, envelope.usage.completion_tokens)
            choice = envelope.choices[0]
            if len(choice.message.content) > MAX_CONTENT_CHARS:
                raise ProviderError("MODEL_OUTPUT_TOO_LARGE", "模型正文超出安全长度。", True)
            if choice.finish_reason == "length":
                raise ProviderError("MODEL_OUTPUT_TRUNCATED", "模型输出被截断，可适当提高输出预算后重试。", True)
            if choice.finish_reason != "stop":
                raise ProviderError("MODEL_OUTPUT_INVALID", "模型未正常完成当前生成。", True)
            content = choice.message.content
            # Never turn a credential echo into a stored narrative or a returned error.
            if key in content:
                raise ProviderError("MODEL_OUTPUT_INVALID", "模型输出包含不允许公开的内容。", True)
            try:
                parsed = _output_json(content)
            except (ValueError, UnicodeError, RecursionError) as error:
                reason = error.reason if isinstance(error, _JSONSyntaxError) else "INVALID_JSON"
                feedback = OutputFeedback("JSON_SYNTAX", (reason,))
                raise ProviderError(
                    "MODEL_OUTPUT_INVALID", _format_message(feedback), True, feedback=feedback,
                ) from None
            try:
                sensitive = _contains_credential(parsed, key)
            except RecursionError:
                feedback = OutputFeedback("JSON_SYNTAX", ("INVALID_JSON",))
                raise ProviderError(
                    "MODEL_OUTPUT_INVALID", _format_message(feedback), True, feedback=feedback,
                ) from None
            if sensitive:
                raise ProviderError("MODEL_OUTPUT_INVALID", "模型输出包含不允许公开的内容。", True)
            if not isinstance(parsed, dict):
                feedback = OutputFeedback("JSON_TYPE", ("ROOT_NOT_OBJECT",))
                raise ProviderError(
                    "MODEL_OUTPUT_INVALID", _format_message(feedback), True, feedback=feedback,
                )
            try:
                value = _validate_output(schema, parsed)
            except ValidationError as error:
                feedback = schema_feedback(error)
                raise ProviderError(
                    "MODEL_OUTPUT_INVALID", _format_message(feedback), True, feedback=feedback,
                ) from None
            except (ValueError, RecursionError):
                feedback = OutputFeedback("SCHEMA_VALIDATION", ("SCHEMA_INVALID",))
                raise ProviderError(
                    "MODEL_OUTPUT_INVALID", _format_message(feedback), True, feedback=feedback,
                ) from None
            return ModelResult(value, envelope.usage.prompt_tokens, envelope.usage.completion_tokens)
        except ProviderError as error:
            if usage is not None:
                error.usage = usage
            # Validation and JSON decoder exceptions can hold the rejected input.
            # Do not attach that context to the safe boundary exception.
            error.__context__ = None
            error.__cause__ = None
            raise
        except httpx.TimeoutException:
            raise ProviderError("PROVIDER_TIMEOUT", "模型连接超时，可保留当前进度后重试。", True) from None
        except httpx.HTTPError:
            raise ProviderError("PROVIDER_CONNECTION_FAILED", "无法连接模型服务，请检查本地配置。", True) from None
        except httpx.InvalidURL:
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "模型服务地址格式无效，请检查本地配置。") from None
        finally:
            if owned:
                await client.aclose()

    @staticmethod
    def _check_status(status: int) -> None:
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            raise ProviderError("PROVIDER_AUTH_FAILED", "模型服务拒绝了认证，请检查本地密钥。")
        if status == 429:
            raise ProviderError("PROVIDER_RATE_LIMITED", "模型服务暂时限流，请稍后重试。", True)
        if status >= 500 or status in {408, 425}:
            raise ProviderError("PROVIDER_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。", True)
        raise ProviderError("PROVIDER_REQUEST_REJECTED", "模型服务拒绝了请求，请检查端点与模型配置。")

    @staticmethod
    def _check_prose(text: str, identifiers: set[str], *, usage: tuple[int, int] | None = None) -> None:
        if not text.strip() or any(identifier and identifier in text for identifier in identifiers):
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", "正文包含不允许的技术引用。", True, usage=usage,
                feedback=OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)),
            )
        if re.search(
            r"(?:参考\s*\d+|condition_fingerprint|source_position_id|based_on_revision|CoT|"
            r"JSON_SYNTAX|JSON_TYPE|SCHEMA_VALIDATION|PROPOSAL_INVALID|PROPOSAL_REFERENCE|"
            r"READING_INVALID|PROSE_INVALID|interface_index|routing_table|object_indices|"
            r"focus_index|expression_indices|reinterprets_indices|reading_table|history_table|"
            r"READING_FOCUS_INVALID|READING_EXPRESSION_INVALID|READING_HISTORY_INVALID|READING_SHARED_REQUIRED|"
            r"revises_index|scope_index|method_index|draft_index|input_fingerprint|"
            r"REFLECTION_REFERENCE_INVALID)", text,
        ):
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", "正文包含不允许的技术引用。", True, usage=usage,
                feedback=OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)),
            )

    async def propose(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Proposal]:
        instructions = f"""
在当前叙述的未完成处生成一份短叙述，并把叙述中的构想直接转用到 peers 的当前条件。
从人物的当前经验生出构想，写出它在同伴生活中的具体作用；牵连体现在各人物的处境和反应中。
本窗口要求多来源共同作用于同一个目标用法，不把单输入的顺序接龙称为联合生成。
若有至少两个 peers，选取他们已有的同一个共同表达与当前作用，
分别向至少两个不同 peers 提出承接，让各来源能在同一目标、表达、作用上共同进入。
若输入有 recommended_shared_interface，优先选择它列出的 interface_indices 和 role，
在其中至少两位不同人物的生活中写出本次构想；不同人物不必经历相同情节。
这些操作从当前叙述中形成，只改变内容与承接，不预先规定剧情；可以额外转用开放角色或主体背景。
输出 fragments 数组，至多 {MAX_PROPOSAL_FRAGMENTS} 个片段；每个片段写一次 text，并附 targets 数组。
无承接的铺垫片段可用 targets: []。所有片段的 targets 总计 1–{MAX_PROPOSAL_OPERATIONS} 项；
每个片段的 targets 至多 {MAX_FRAGMENT_TARGETS} 项。每项只写 interface_index 和 role；
interface_index 必须是本次 routing_table 中已有的整数，准确选择该行对应的人物和表达。
程序将选中的接口编译为承接目标；不要自行输出人物ID、姓名、表达ID或用法ID作为目标。
这些整数只在本次请求有效，不沿用过去的编号；正文里的相关人物仍使用自然姓名。
role 沿用当前作用或给出相容的开放作用；多个 targets 可直接承接同一片段。
routing_table 只有可写接口及现有作用，不提供同伴的私有背景原文。允许转用目标的主体背景，
让本次表达参与目标如何构成自身；这种变更保持局部归属，不裁定全网共同的真实身份或过去。
有 targets 的片段 text 将原样成为这些目标人物那里实际成立的内容，写清对应人物和正在发生的生活。
不要额外输出 target_position_id、expression_id、value、anchor、position_id、based_on_revision
或 operations，也不要重复抄写原文。
有承接的单片段至多 {MAX_OPERATION_TEXT_CHARS} 字符，拼接后的全部叙述（含换行）至多
{MAX_PROPOSAL_TEXT_CHARS} 字符；按生活的需要决定长度，不必用满上限。
只构想下一处正在发生的生活，不告诉人物它是模型、被控制或被技术生成。
"""
        if frame.reflection is not None:
            instructions = f"""
根据本次实际材料与组织方式形成以当前人物为主角的短叙述。构想可继续生活、改变材料之间的
联系，或生出新的组织方法。没有每回合联合写入、转用同伴或修改方法的义务。
输出 fragments 数组；每个片段写 text 和 targets。targets 可为空；若提出普通承接，
interface_index 仅选 routing_table 中本次整数，role 给出实际作用。程序逐字采用片段正文。
有承接片段至多 {MAX_OPERATION_TEXT_CHARS} 字符，拼接正文至多 {MAX_PROPOSAL_TEXT_CHARS} 字符。
不要输出 operations、持久目标ID、value 或 anchor。正文只呈现生活，技术选择位于结构字段。
""" + REFLECTION_INSTRUCTIONS
        data = _actor_frame(frame)
        if frame.reflection is not None:
            # Classic background fields would bypass the method's actual selection.
            data.pop("seed_text", None)
            data.pop("active_uses", None)
            data.pop("recent_surface", None)
            data["reflection"] = reflection_data(frame.reflection)
        # All routing pairs are derived from the same immutable local frame used
        # for this evaluation. Never resolve handles against later world state.
        routing_table = _routing_table(frame)
        data["routing_table"] = routing_table
        shared_interface = _shared_interface(frame)
        recommended_indices = []
        if shared_interface is not None and frame.reflection is None:
            recommended_indices = [
                route["interface_index"] for route in routing_table
                if route["target_position_id"] in shared_interface["target_position_ids"]
                and route["expression_id"] == shared_interface["expression_id"]
            ]
            data["recommended_shared_interface"] = {
                "interface_indices": recommended_indices,
                "role": shared_interface["role"],
            }
        output_schema = _proposal_schema(frame)
        # Without a recommended common role, still show two different peers,
        # rather than accidentally selecting two expressions for the same peer.
        example_indices = recommended_indices[:2]
        if not example_indices:
            seen_peers = set()
            for route in routing_table:
                if route["target_position_id"] not in seen_peers:
                    example_indices.append(route["interface_index"])
                    seen_peers.add(route["target_position_id"])
                if len(example_indices) == 2:
                    break
        targets = [
            {
                "interface_index": interface_index,
                "role": shared_interface["role"] if shared_interface else (
                    frame.active_uses[0].get("role", "共同生活") if frame.active_uses else "共同生活"
                ),
            }
            for interface_index in example_indices
        ]
        result = await self._request(
            instructions, data, _ReflexiveFragmentProposal if frame.reflection else _ProposalResponse,
            output_schema=output_schema,
            output_example={"fragments": [{"text": _PROSE_PLACEHOLDER,
                                           "targets": [] if frame.reflection else targets}]},
            repair=repair,
        )
        usage = (result.input_tokens, result.output_tokens)

        def invalid(reason: str) -> None:
            # Fixed categories only: never include rejected prose, identifiers or validation details.
            raise ProviderError(
                "MODEL_OUTPUT_INVALID", f"模型提出了无效的承接操作（{reason}）。", True, usage=usage,
                feedback=OutputFeedback(
                    "PROPOSAL_REFERENCE" if reason in {
                        "TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE", "INTERFACE_UNKNOWN",
                    } else "PROPOSAL_INVALID",
                    (reason,),
                ),
            )

        wire = result.value if frame.reflection else result.value.root
        if isinstance(wire, Proposal):
            value = wire
        else:
            operations = []
            for fragment in wire.fragments:
                if not fragment.text.strip():
                    invalid("EMPTY_FRAGMENT")
                if fragment.targets and len(fragment.text) > MAX_OPERATION_TEXT_CHARS:
                    invalid("FRAGMENT_TOO_LONG")
                for target in fragment.targets:
                    if isinstance(target, _FragmentTarget):
                        if not 0 <= target.interface_index < len(routing_table):
                            invalid("INTERFACE_UNKNOWN")
                        route = routing_table[target.interface_index]
                        target_position_id = route["target_position_id"]
                        expression_id = route["expression_id"]
                    else:
                        target_position_id = target.target_position_id
                        expression_id = target.expression_id
                    # Mechanical compilation uses the model's prose verbatim. There is no
                    # inferred replacement, anchor repair, omitted target or extra evaluation.
                    operations.append(Operation(
                        op="rebind",
                        target_position_id=target_position_id,
                        expression_id=expression_id,
                        role=target.role,
                        value=fragment.text,
                        anchor=fragment.text,
                    ))
            if not operations and frame.reflection is None:
                invalid("NO_PEER_OPERATION")
            if len(operations) > MAX_PROPOSAL_OPERATIONS:
                invalid("TOO_MANY_OPERATIONS")
            text = "\n".join(fragment.text for fragment in wire.fragments)
            if len(text) > MAX_PROPOSAL_TEXT_CHARS:
                invalid("PROPOSAL_TOO_LARGE")
            value = Proposal(
                position_id=frame.position_id, based_on_revision=frame.revision,
                text=text, operations=operations,
                reflection=compile_reflection(wire.reflection, frame.reflection, usage=usage)
                if frame.reflection else None,
            )
        peers = {peer["id"] for peer in frame.peers} - {frame.position_id}
        expressions = _local_route_expressions(frame)
        interfaces = {(entry["position_id"], entry["expression_id"]) for entry in frame.peer_interfaces}
        if value.position_id != frame.position_id:
            invalid("POSITION_MISMATCH")
        if value.based_on_revision != frame.revision:
            invalid("REVISION_MISMATCH")
        if frame.reflection is None and value.reflection is not None:
            invalid("REFLECTION_REFERENCE_INVALID")
        if not value.operations and frame.reflection is None:
            invalid("NO_PEER_OPERATION")
        for operation in value.operations:
            if operation.target_position_id not in peers:
                invalid("TARGET_UNKNOWN")
            if (
                operation.expression_id not in expressions
                and (operation.target_position_id, operation.expression_id) not in interfaces
            ):
                invalid("EXPRESSION_UNAVAILABLE")
            if not operation.anchor.strip() or operation.anchor not in value.text:
                invalid("ANCHOR_NOT_IN_TEXT")
        identifiers = peers | expressions | {frame.position_id} | {expression for _, expression in interfaces}
        if frame.reflection is not None:
            identifiers |= {row["id"] for row in frame.reflection.objects}
            identifiers |= {row["id"] for row in frame.reflection.scopes}
        self._check_prose(value.text, identifiers, usage=usage)
        for operation in value.operations:
            self._check_prose(operation.value, identifiers, usage=usage)
        return ModelResult(value, result.input_tokens, result.output_tokens)

    async def realize(
        self, frame: GenerationFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[str]:
        instructions = f"""
从已经重构的 active_uses 条件继续当前未完成 seed_text，输出 {{"text":"自然语言正文"}}。
正文为第三人称叙述，承接正在生效的 values，尤其同一表达上的多个 values。
从这些条件共同造成的处境继续，写人物怎样经历和回应。以有联系的经验组织段落，
把已有事实作为生活的来处，把本次篇幅留给它们此刻怎样起作用。
text 至多 {MAX_REALIZATION_TEXT_CHARS} 字符，按需要决定段落与篇幅，不必用满上限。
这次变化应影响人物当前如何继续；条件通过实际经验进入正文。
"""
        data = _actor_frame(frame)
        if frame.reflection is not None:
            instructions = f"""
从本次 materials 的实际选择和 instruction 的组织方式继续当前人物的生活。
这些材料共同形成当前处境，选择与组织的后果落实在行动、感知和关系中。
只输出 {{"text":"自然语言正文"}}，至多 {MAX_REALIZATION_TEXT_CHARS} 字符；不输出方法操作。
材料没有给出的旧背景不自动补回，材料之间的差异保留局部意义。正文使用人物自然姓名。
"""
            data = {
                "name": frame.name, "materials": materials_data(frame.reflection),
                "groups": frame.reflection.groups, "instruction": frame.reflection.instruction,
            }
        result = await self._request(
            instructions, data, _Surface,
            output_example={"text": _PROSE_PLACEHOLDER}, repair=repair,
        )
        identifiers = {frame.position_id} | {peer["id"] for peer in frame.peers}
        identifiers |= {use["expression_id"] for use in frame.active_uses}
        identifiers |= {interface["expression_id"] for interface in frame.peer_interfaces}
        if frame.reflection is not None:
            identifiers |= {row["id"] for row in frame.reflection.objects}
            identifiers |= {row["id"] for row in frame.reflection.scopes}
        self._check_prose(result.value.text, identifiers, usage=(result.input_tokens, result.output_tokens))
        return ModelResult(result.value.text, result.input_tokens, result.output_tokens)

    async def present(
        self, frame: ReadingFrame, *, repair: OutputFeedback | None = None,
    ) -> ModelResult[Presentation]:
        # Preserve exactly the request's scopes, entry, history and selected methods
        # while the asynchronous transport runs. Resolve all handles in this copy.
        frame = frame.model_copy(deep=True)
        table = reading_table(frame)
        instructions = f"""
你是一个局部阅读位置。选择 candidates 中正在生效的一处叙述，形成下一段可读正文。
这是同一世界中的一条独立连续阅读。previous_segments 是这条阅读已经呈现的全部可用历史。
start_position_id 是这条阅读的最初立足点；首段 required_focus_position_id 非空时，必须从该人物处进入。
起点决定从何处看见这片生活，不预设独立的新世界，不要求之后固定在此人物。
后续按 current_focus、open_relations、正在生效的共同用法以及本条阅读的未完之处决定如何转移焦点。
让所选位置的 active_uses 中的 values 实际影响人物怎样继续；相互牵连应发生在正文里。
沿已有共同表达的用法展开，把本条阅读的前文作为已经发生的经验，按当前牵连选择值得呈现的部分。
让焦点与段落衔接承接生活中的关系、感知或后果；此前的片段可以由本次经验获得新的读法。
可以保留同一表达的不同归属与不完全重合的经历，按生活的节奏决定是否转移焦点。
focus_index 仅选择 reading_table 中已有的整数；该表已限定本条阅读的可用焦点及起始入口。
expression_indices 仅选择所选焦点行 expressions 中的 expression_index，至少含一项 shared=true。
reinterprets_indices 可以为空，否则仅选 history_table 的 history_index，正文应实际参与重读。
程序将这些整数编译为准确的焦点、表达和本条历史引用；编号仅在本次请求有效。
不要自行输出 focus_position_id、expression_ids、reinterprets，或复制姓名、对象ID作为整数引用。
text 不含段落编号、节点编号、括号引用、诊断说明或来源标注。
text 至多 {MAX_PRESENTATION_TEXT_CHARS} 字符，按需要决定段落与篇幅，不必用满上限。
"""
        if frame.reflection is not None:
            instructions = f"""
你是独立连续阅读的位置。依据本次选中 materials 和 instruction 形成下一段生活，
按相互作用组织呈现，使此前的经历在当前关系中继续。首段仍从 required_focus_position_id 进入。
focus_index 仅选 reading_table 中已有的整数；起始入口已由该表限定。
expression_indices 仅选该焦点行 expressions 中的 expression_index，允许为空。
reinterprets_indices 可为空，否则仅选 history_table 的 history_index。
程序精确编译引用；这些整数仅本次有效，不输出 focus_position_id、expression_ids、reinterprets
或自行拼写人物、表达、历史段落ID。正文使用自然姓名，技术选择位于结构字段。
可以从当前经验提出新的材料组织方式，经 reflection 字段返回世界；无需每次提出。
text 至多 {MAX_PRESENTATION_TEXT_CHARS} 字符；正文没有编号、方法说明或技术引用。
""" + REFLECTION_INSTRUCTIONS
        data = {
            "revision": frame.revision,
            "start_position_id": frame.start_position_id,
            "required_focus_position_id": required_focus(frame),
            "current_focus": frame.current_focus,
            "shared_expression_ids": sorted(shared_expression_ids(frame)),
            "reading_table": table,
            "history_table": history_table(frame),
            "previous_segments": [
                {
                    key: segment[key]
                    for key in ("id", "revision", "text", "focus_position_id", "expression_ids", "reinterprets")
                    if key in segment
                }
                for segment in frame.previous_segments
            ],
        }
        # Candidates and relations remain actor-safe even if future callers add diagnostic fields.
        data["candidates"] = [
            {
                "position_id": candidate["position_id"],
                "name": candidate["name"],
                "text": candidate["text"],
                "active_uses": [
                    {key: use[key] for key in ("expression_id", "text", "role", "values") if key in use}
                    for use in candidate.get("active_uses", [])
                ],
            }
            for candidate in frame.candidates
        ]
        data["open_relations"] = [
            {
                key: relation[key]
                for key in ("expression_id", "role", "position_id", "position_ids")
                if key in relation
            }
            for relation in frame.open_relations
        ]
        if frame.reflection is not None:
            data["reflection"] = reflection_data(frame.reflection)
            data["candidates"] = [
                {
                    "position_id": candidate["position_id"], "name": candidate["name"],
                    "active_uses": [
                        {key: use[key] for key in ("expression_id", "role") if key in use}
                        for use in candidate.get("active_uses", [])
                    ],
                }
                for candidate in frame.candidates
            ]
        example_focus = table[0]
        example_expressions = [expression["expression_index"]
                               for expression in example_focus["expressions"]
                               if frame.reflection is not None or expression["shared"]][:1]
        result = await self._request(
            instructions, data,
            _ReflexivePresentationResponse if frame.reflection else _PresentationResponse,
            output_schema=_presentation_schema(frame),
            output_example={
                "text": _PROSE_PLACEHOLDER,
                "focus_index": example_focus["focus_index"],
                "expression_indices": example_expressions, "reinterprets_indices": [],
            }, repair=repair,
        )
        wire = result.value.root
        usage = (result.input_tokens, result.output_tokens)
        references = compile_reading_references(
            frame, wire.focus_index, wire.expression_indices, wire.reinterprets_indices, usage=usage,
        ) if isinstance(wire, _IndexedPresentation) else wire.model_dump(
            include={"focus_position_id", "expression_ids", "reinterprets"},
        )
        value = Presentation(
            text=wire.text, **references,
            reflection=compile_reflection(
                    wire.reflection, frame.reflection, usage=usage,
                ) if frame.reflection else None,
        )
        positions = {candidate["position_id"] for candidate in frame.candidates}
        expressions = {
            use["expression_id"]
            for candidate in frame.candidates
            for use in candidate.get("active_uses", [])
        }
        segments = {segment["id"] for segment in frame.previous_segments}
        if frame.reflection is not None:
            segments |= {row["id"] for row in frame.reflection.objects}
            segments |= {row["id"] for row in frame.reflection.scopes}
        validate_presentation(frame, value, usage=(result.input_tokens, result.output_tokens))
        self._check_prose(
            value.text, positions | expressions | segments, usage=(result.input_tokens, result.output_tokens)
        )
        return ModelResult(value, result.input_tokens, result.output_tokens)
