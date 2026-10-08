"""Version-two program wire language with request-owned object capabilities.

The public program uses integer object handles. Only compilation resolves them
against the frozen frame; step names remain local to one declarative program.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opentale.reflection_types import ReflectionFrame

from .base import ProviderError
from .feedback import OutputFeedback
from .reflection import WireAdoption, WireMethod


class WireRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WireObjectRef(WireRecord):
    object_index: int
    mode: Literal["live", "version"] = "live"
    version: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class WireRead(WireRecord):
    id: str
    op: Literal["read"]
    refs: list[WireObjectRef]


class WireQuery(WireRecord):
    id: str
    op: Literal["query"]
    query: str
    kinds: list[Literal["content", "method", "choice", "view", "response", "effect"]] = Field(default_factory=list)
    related_to: list[WireObjectRef] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)


class WireGroup(WireRecord):
    id: str
    op: Literal["group"]
    inputs: list[str]
    label: str = ""


class WireRelation(WireRecord):
    source: str
    target: str
    label: str


class WireRelate(WireRecord):
    id: str
    op: Literal["relate"]
    inputs: list[str]
    relations: list[WireRelation]


class WireInspect(WireRecord):
    id: str
    op: Literal["inspect"]
    ref: WireObjectRef


class WireMethodPatch(WireRecord):
    title: str | None = None
    instruction: str | None = None
    object_indices: list[int] | None = None
    order: Literal["listed", "reverse"] | None = None
    limit: int | None = Field(default=None, ge=1)
    program: WireProgram | None = None

    @model_validator(mode="after")
    def explicit_patch_values(self):
        for name in self.model_fields_set - {"limit"}:
            if getattr(self, name) is None:
                raise ValueError("Patch fields must have a value when present")
        return self


class WireRevise(WireRecord):
    id: str
    op: Literal["revise"]
    input: str
    patch: WireMethodPatch


class WireApply(WireRecord):
    id: str
    op: Literal["apply"]
    method: str


WireStep = Annotated[
    WireRead | WireQuery | WireGroup | WireRelate | WireInspect | WireRevise | WireApply,
    Field(discriminator="op"),
]


class WireProgram(WireRecord):
    language_version: Literal[2] = 2
    steps: list[WireStep] = Field(min_length=1)
    output: str


WireMethodPatch.model_rebuild()
WireRevise.model_rebuild()
WireProgram.model_rebuild()


class WireMethodV2(WireRecord):
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(default="", max_length=1200)
    language_version: Literal[2] = 2
    program: WireProgram
    revises_index: int | None = None


class WireResponse(WireRecord):
    text: str = Field(min_length=1)
    about_indices: list[int] = Field(min_length=1)
    evidence_indices: list[int] = Field(default_factory=list)
    visibility: Literal["scope", "shared"] = "scope"


class WireReflectionV2(WireRecord):
    methods: list[WireMethodV2 | WireMethod] = Field(default_factory=list)
    adoptions: list[WireAdoption] = Field(default_factory=list)
    responses: list[WireResponse] = Field(default_factory=list)


def _invalid(usage: tuple[int, int] | None) -> None:
    raise ProviderError(
        "MODEL_OUTPUT_INVALID", "模型提出了无效的方法引用。", True, usage=usage,
        feedback=OutputFeedback("PROPOSAL_INVALID", ("REFLECTION_REFERENCE_INVALID",)),
    )


def compile_program(wire: WireProgram, frame: ReflectionFrame, *, usage=None, exposed: dict | None = None) -> dict:
    """Produce core program data without assigning meaning to generated labels."""
    def object_id(index: int, kind: str | None = None) -> str:
        if type(index) is not int or not 0 <= index < len(frame.objects):
            _invalid(usage)
        if exposed is not None and index not in exposed["objects"]:
            _invalid(usage)
        row = frame.objects[index]
        if kind is not None and row["kind"] != kind:
            _invalid(usage)
        return row["id"]

    def ref(value: WireObjectRef, kind: str | None = None) -> dict:
        identity = object_id(value.object_index, kind)
        row = frame.objects[value.object_index]
        version = row.get("version") if value.mode == "version" else None
        if value.version is not None and value.version != version:
            _invalid(usage)
        if value.mode == "version" and not version:
            _invalid(usage)
        if value.start is not None and value.end is not None and value.end < value.start:
            _invalid(usage)
        return {"object_id": identity, "mode": value.mode, "version": version,
                "start": value.start, "end": value.end}

    steps = []
    for step in wire.steps:
        data = step.model_dump(exclude_unset=True)
        if isinstance(step, WireRead):
            data["refs"] = [ref(value) for value in step.refs]
        elif isinstance(step, WireQuery):
            data["related_to"] = [ref(value) for value in step.related_to]
        elif isinstance(step, WireInspect):
            data["ref"] = ref(step.ref, "method")
        elif isinstance(step, WireRevise):
            patch = step.patch.model_dump(exclude_unset=True)
            if "object_indices" in patch:
                indices = patch.pop("object_indices")
                if indices is None:
                    _invalid(usage)
                patch["inputs"] = [object_id(index) for index in indices]
            if step.patch.program is not None:
                patch["program"] = compile_program(step.patch.program, frame, usage=usage, exposed=exposed)
            data["patch"] = patch
        steps.append(data)
    return {"steps": steps, "output": wire.output}


def program_projection(program: dict, indices: dict[str, int]) -> dict:
    """Render definitions with local handles, excluding absent/private identities.

    Unavailable dependencies stay explicit. Such previews are descriptions, not
    executable new programs; a model can inspect the permitted method itself.
    """
    steps = []

    def ref(value: dict) -> dict:
        index = indices.get(value.get("object_id", ""))
        if index is None:
            return {"unavailable": True}
        return {"object_index": index, **{key: value[key] for key in
                ("mode", "version", "start", "end") if key in value and value[key] is not None}}

    for source in program.get("steps", []):
        op = source.get("op")
        step = {"id": source.get("id", ""), "op": op}
        if op == "read":
            step["refs"] = [ref(value) for value in source.get("refs", [])]
        elif op == "query":
            step.update({key: source[key] for key in ("query", "kinds", "limit") if key in source})
            step["related_to"] = [ref(value) for value in source.get("related_to", [])]
        elif op == "inspect":
            step["ref"] = ref(source.get("ref", {}))
        elif op == "revise":
            step["input"] = source.get("input", "")
            patch = source.get("patch", {})
            step["patch"] = {key: patch[key] for key in ("title", "instruction", "order", "limit")
                             if key in patch}
            if "inputs" in patch and patch["inputs"] is not None:
                step["patch"]["object_indices"] = [indices[value] for value in patch["inputs"]
                                                   if value in indices]
                step["patch"]["unavailable_input_count"] = sum(value not in indices for value in patch["inputs"])
            if patch.get("program") is not None:
                step["patch"]["program"] = program_projection(patch["program"], indices)
        elif op == "apply":
            step["method"] = source.get("method", "")
        elif op in {"group", "relate"}:
            step["inputs"] = list(source.get("inputs", []))
            if op == "group":
                step["label"] = source.get("label", "")
            else:
                step["relations"] = [{key: relation[key] for key in ("source", "target", "label")
                                      if key in relation} for relation in source.get("relations", [])]
        steps.append(step)
    return {"steps": steps, "output": program.get("output", "")}


V2_REFLECTION_INSTRUCTIONS = """
本次 reflection.language_version=2，可直接生成有类型的方法程序。
reflection.materials 和 view 保存本次实际采用的材料、嵌套分组与关系；objects 提供可操作对象。
executed_definitions 保存实际检查和执行过的完整方法定义，operation 区分 inspect 与 apply；
transient_definition 表示执行中产生的临时定义，可读其内容并构拟新方法，引用仍使用 objects。
方法写 title、instruction、language_version:2、program:{steps:[...],output:"步骤名"}。
每步 id 为本程序内唯一名称；inputs/input/method/source/target 引用本程序内已经定义的步骤。
read 步骤 refs 读取对象全文或 start/end 字符范围；每个 ref 使用本次 object_index。
ref.mode=live 在每次执行时取当前对象；mode=version 冻结本次对象版本，version 可省略交由程序填入。
query 步骤 query 写具体检索词，kinds 可选 content/method/choice/view/response/effect，
related_to 可选对象引用，limit:null 读取全部命中；检索范围由当前作用域决定。
group 步骤将 inputs 对应视图组成带 label 的嵌套组；relate 步骤并置 inputs，relations 每项
用 source、target 两个已有步骤名与自然语言 label 表达关系。
inspect 步骤 ref 选已有 method，取得完整定义；revise 步骤 input 选 inspect/revise 结果，
patch 按需要设置 instruction、title、order、limit、object_indices 或 program。
apply 步骤 method 选 inspect/revise 的结果，真实执行该定义并返回视图；program.output
选择一个实际视图步骤作为生成输入。方法定义的检查本身不等于执行。
revises_index 可选已有 method。adoptions 的 scope_index 选本次 scopes；method_index
选已有方法或 draft_index 选本次 methods 草稿，二者择一；action 为 adopt 或 release。
reflection.responses 按需写局部回应：text 描述相关组织怎样作用于当前经验；about_indices
指出回应对象，evidence_indices 指出实际依据；visibility=scope 保留本位置，shared 使其他
有权限的位置可取用。这些是可继续理解的局部陈述；生活正文写在本阶段正文专用字段。
effect 对象记录实际输入组织的前后变化，可读取、检索并作为回应的对象或依据。
这些记录说明采用和组织实际怎样变化；其叙事含义由各位置结合经历继续理解。
方法、回应及阅读都可进入后来组织；采用后的方法才改变后续实际输入。根据当前需要决定
是否修改、回应或沿用。各处可以保留不同理解，方向由具体经验中的持续选择形成。
选材与组织围绕当前未完成的生活展开。原文保留整段关系，可全文读取并重新进入旧组织。
正文使用自然姓名与生活语言；对象编号、步骤名、程序定义放在结构字段。
""".strip()
