"""Version 3 context virtualization with a measured hard serialization bound.

The frozen frame is the backing store. Full lossless input is preferred; oversized
inputs become explicit pages whose original indices remain stable. Every admitted
block is measured together with the finite schema and reserved repair space.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from typing import Callable

from opentale.capacity import MAX_MODEL_REQUEST_CHARS

from .context import REPAIR_RESERVE_CHARS, compact_context, compact_json, compact_schema

PROJECTION_VERSION = "provider-json-v3-bounded"
ALGORITHM_VERSION = "source-pages-v1"

CONTEXT_INSTRUCTIONS = """
本次使用可寻址的上下文页面，原始实验、材料和视图完整保存在冻结输入中。
text_ref 对应 text_pool 中同名 text_index；metadata_ref 对应 material_metadata 的 metadata_index。
这些表可能是稀疏页面，按字段索引查找，数组下标不代表原索引。材料若有 material_index、历史
若有 history_index，均指原出现位置；它们与视图的 material_indices 保持同一原索引。
同一候选的相同(expression_id,role)可以合并显示，occurrence_count 保留其出现次数。
spans 与 segments 一一对应原文范围，omitted_chars 标明未读部分；partial_sentence=true
表示字符范围在句中截断，须结合原文确认完整命题。依据可见文字生成，需要时用 read 的
start/end 或 query 收窄原始材料；未展示的旧对象仍可经后续检索或完整对象接口查看。
context_compaction.pages 说明省略的集合和来源承诺；structure_excerpted=true 时，当前页面
只展示部分结构，保留各处的局部归属，不宣称已经读过整个视图。executed_definitions 可以
通过 definition_ref 引用 definition_pool；带 detail_excerpted 的定义只是预览。
instruction_span 与 instruction_original_chars 标明本页组织方向的原文范围；方向说明
优先保留，剩余部分按其页面标记待取回。完整视图可以引用尚未展示的材料原索引，这些
材料的出现位置仍在冻结输入中；按材料页面标记区分当前可见与待取回部分。
所有输出接口仅从本页 context_capabilities 与可见表选择，整数仍为原接口编号。对象、历史
或路由没有展示时，使用已展示的接口或通过方法 query 取得后续材料，不猜测隐藏编号。
正文仍填写自然语言 text；这些池、页标记和来源索引属于输入表示，保留在正文之外。
""".strip()


def _hash(value: object) -> str:
    return hashlib.sha256(compact_json(value).encode()).hexdigest()


def _normalize(data: dict) -> dict:
    result = deepcopy(data)
    for candidate in result.get("candidates", []):
        uses = candidate.get("active_uses", [])
        counts = Counter(compact_json(use) for use in uses)
        candidate["active_uses"] = [{**json.loads(value), **({"occurrence_count": count} if count > 1 else {})}
                                    for value, count in counts.items()]
    return result


def capabilities(data: dict) -> dict:
    reflection = data.get("reflection", {})
    return {
        "objects": [row["object_index"] for row in reflection.get("objects", [])],
        "scopes": [row["scope_index"] for row in reflection.get("scopes", [])],
        "routes": [row["interface_index"] for row in data.get("routing_table", [])],
        "focuses": [row["focus_index"] for row in data.get("reading_table", [])],
        "expressions": {str(row["focus_index"]): [value["expression_index"] for value in row.get("expressions", [])]
                        for row in data.get("reading_table", [])},
        "history": [row["history_index"] for row in data.get("history_table", [])],
    }


def _schema_base(schema: dict) -> dict:
    result = deepcopy(schema)
    # Dynamic handle enumerations are all rebuilt below from admitted rows.
    def strip(value):
        if isinstance(value, list):
            return [strip(item) for item in value]
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items()
                    if not (key == "enum" and isinstance(item, list) and all(type(x) is int for x in item))}
        return value
    result = strip(result)
    if "focus_index" in result.get("properties", {}):
        result.pop("oneOf", None)
    return result


def bound_schema(base: dict, data: dict) -> dict:
    result = deepcopy(base)
    defs = result.get("$defs", {})
    caps = capabilities(data)
    def integer(values):
        return {"type": "integer", "enum": values}
    methods = [row["object_index"] for row in data.get("reflection", {}).get("objects", [])
               if row.get("kind") == "method"]
    for name in ("WireMethod",):
        if name in defs:
            defs[name]["properties"]["object_indices"]["items"] = integer(caps["objects"])
    for name in ("WireMethod", "WireMethodV2"):
        if name in defs:
            defs[name]["properties"]["revises_index"] = {"anyOf": [integer(methods), {"type": "null"}]}
    if "WireObjectRef" in defs:
        defs["WireObjectRef"]["properties"]["object_index"] = integer(caps["objects"])
    if "WireMethodPatch" in defs:
        defs["WireMethodPatch"]["properties"]["object_indices"] = {"type": "array", "items": integer(caps["objects"])}
    if "WireResponse" in defs:
        for key in ("about_indices", "evidence_indices"):
            defs["WireResponse"]["properties"][key]["items"] = integer(caps["objects"])
    if "WireAdoption" in defs:
        defs["WireAdoption"]["properties"]["scope_index"] = integer(caps["scopes"])
        defs["WireAdoption"]["properties"]["method_index"] = {"anyOf": [integer(methods), {"type": "null"}]}
    for name in ("_FragmentTarget", "_FragmentTargetV2"):
        if name in defs:
            defs[name]["properties"]["interface_index"] = integer(caps["routes"])
    if "focus_index" in result.get("properties", {}):
        result["properties"]["focus_index"] = integer(caps["focuses"])
        result["properties"]["reinterprets_indices"]["items"] = integer(caps["history"])
        result["oneOf"] = [{"properties": {
            "focus_index": {"const": focus},
            "expression_indices": {"items": integer(caps["expressions"][str(focus)])},
        }} for focus in caps["focuses"]]
    return compact_schema(result)


def _preview(value: object, *, depth: int = 0) -> object:
    """A finite preview block. Its enclosing page descriptor marks omitted detail."""
    if isinstance(value, str):
        return value[:160]
    if isinstance(value, list):
        return [_preview(item, depth=depth + 1) for item in value[:6]] if depth < 4 else []
    if isinstance(value, dict):
        if depth >= 4:
            return {}
        return {key: _preview(item, depth=depth + 1) for key, item in list(value.items())[:24]}
    return value


def prepare_bounded_context(
    data: dict, schema: dict, *, system_builder: Callable[[dict], str], input_chars: int, target_chars: int,
) -> tuple[dict, dict]:
    source = _normalize(data)
    base = _schema_base(schema)
    # Reuse the frozen v2 *lossless* representation, not its heuristic excerpt loop.
    full = compact_context(source, system_chars=0, input_chars=input_chars, target_chars=10**30)
    stats = full["context_compaction"]
    original_materials = (full.get("reflection") or full).get("materials", [])
    original_objects = full.get("reflection", {}).get("objects", [])
    original_history = full.get("previous_segments", [])
    stats.update(algorithm=ALGORITHM_VERSION, projection_version=PROJECTION_VERSION,
                 target_chars=target_chars, structure_excerpted=False,
                 material_occurrences_visible=len(original_materials), candidate_objects_visible=len(original_objects),
                 history_segments_visible=len(original_history), pages=[])
    full["context_capabilities"] = capabilities(full)
    hard_limit = MAX_MODEL_REQUEST_CHARS - REPAIR_RESERVE_CHARS

    def measure(value: dict) -> tuple[int, dict]:
        value["context_capabilities"] = capabilities(value)
        shaped = bound_schema(base, value)
        current = value["context_compaction"]
        for _ in range(5):
            length = len(system_builder(shaped)) + len(compact_json(value))
            if current["output_chars"] == length:
                return length, shaped
            current["output_chars"] = length
        return current["output_chars"], shaped

    size, shaped = measure(full)
    if size <= hard_limit:
        stats["within_target"] = size <= target_chars
        _, shaped = measure(full)
        return full, shaped

    # The bounded core has no unbounded collection. At least one legal reading
    # focus survives; reflexive expressions/targets/changes may all be empty.
    result = {key: _preview(source[key]) for key in ("revision", "name", "position_id", "language_version")
              if key in source}
    result.update(text_pool=[], context_compaction=deepcopy(stats))
    result["context_compaction"].update(structure_excerpted=True, pages=[])
    if "reflection" in full:
        result["reflection"] = {"language_version": 2, "objects": [], "scopes": [], "materials": [],
                                "instruction": "", "view": {"groups": [], "relations": [], "material_indices": []},
                                "active_method_indices": [], "selected_indices": [],
                                "executed_definitions": [], "definition_pool": []}
    else:
        result.update(materials=[], instruction="", view={"groups": [], "relations": [], "material_indices": []})
    if "routing_table" in full:
        result["routing_table"] = []
    if "reading_table" in full:
        first = full["reading_table"][0]
        result.update(reading_table=[{"focus_index": first["focus_index"], "name": str(first["name"])[:80],
                                      "expressions": []}], history_table=[], previous_segments=[], candidates=[])
    owner = result.get("reflection", result)
    pages: dict[str, dict] = {}

    def page(path: str, original: object, total: int, visible: list[int] | None = None) -> dict:
        value = {"path": path, "total": total, "omitted_count": total, "sha256": _hash(original)}
        if visible is not None:
            value["visible_indices"] = visible
            value["omitted_count"] -= len(visible)
        pages[path] = value
        result["context_compaction"]["pages"] = list(pages.values())
        return value

    material_path = "reflection.materials" if "reflection" in full else "materials"
    material_page = page(material_path, (data.get("reflection") or data).get("materials", []), len(original_materials), [])
    page("reflection.objects", data.get("reflection", {}).get("objects", []), len(original_objects), [])
    history_page = page("previous_segments", data.get("previous_segments", []), len(original_history), [])
    view_source = (full.get("reflection") or full).get("view") or {}
    page("reflection.view" if "reflection" in full else "view", view_source, max(1, len(view_source.get("groups", []))))
    for key in ("routing_table", "reading_table", "candidates", "open_relations"):
        if key in full:
            page(key, data.get(key, []), len(full[key]))
    if "reflection" in full:
        page("reflection.scopes", full["reflection"]["scopes"], len(full["reflection"]["scopes"]))
        page("reflection.executed_definitions", full["reflection"]["executed_definitions"],
             len(full["reflection"]["executed_definitions"]))
    source_owner = full.get("reflection", full)
    active_source = set(source_owner.get("active_method_indices", []))
    selected_counts = Counter(source_owner.get("selected_indices", []))
    instruction = source_owner.get("instruction", "")
    owner["instruction"] = instruction[:4096]
    owner["instruction_span"] = [0, len(owner["instruction"])]
    owner["instruction_original_chars"] = len(instruction)
    instruction_page = page("reflection.instruction" if "reflection" in full else "instruction",
                            instruction, len(instruction))
    instruction_page["omitted_count"] = len(instruction) - len(owner["instruction"])
    # Soft goals below the fixed interface size yield to a useful minimum working
    # context. All block admissions still use the physical hard bound.
    working_limit = min(hard_limit - 1024, max(64_000, target_chars))
    initial, _ = measure(result)
    working_limit = min(hard_limit - 1024, max(working_limit, initial + 16_000))
    pool_by_index = {entry["text_index"]: entry for entry in full["text_pool"]}
    metadata = {row["metadata_index"]: row for row in full.get("material_metadata", [])}
    included_texts: dict[int, dict] = {}

    def add_text(row: dict, path: str, original_index: int, allowance: int) -> tuple[dict, tuple]:
        ref = row["text_ref"]
        original = pool_by_index[ref]
        source_ref = {"path": path, "index": original_index}
        if ref in included_texts:
            included_texts[ref]["sources"].append(source_ref)
            undo = (ref, False)
        else:
            text = "".join(original["segments"])
            take = min(len(text), allowance)
            head, tail = (take + 1) // 2, take // 2
            spans = [[0, len(text)]] if take == len(text) and text else (
                [[0, head], [len(text) - tail, len(text)]] if tail else ([[0, head]] if head else []))
            entry = {**original, "spans": spans, "segments": [text[start:end] for start, end in spans],
                     "omitted_chars": len(text) - take, "partial_sentence": take < len(text), "sources": [source_ref]}
            result["text_pool"].append(entry)
            included_texts[ref] = entry
            undo = (ref, True)
        material = {key: value for key, value in row.items() if key not in {"metadata_ref", "text_ref"}}
        if "metadata_ref" in row:
            material = {key: value for key, value in metadata[row["metadata_ref"]].items() if key != "metadata_index"}
        if any(isinstance(value, str) and len(value) > 160 for value in material.values()):
            material = {"metadata_excerpted": True}
        material.update(text_ref=ref, **({"material_index": original_index} if path != "previous_segments"
                                       else {"history_index": original_index}))
        return material, undo

    def undo_text(undo: tuple) -> None:
        ref, created = undo
        if created:
            result["text_pool"].remove(included_texts.pop(ref))
        else:
            included_texts[ref]["sources"].pop()

    def sync_references() -> None:
        if "reflection" in result:
            visible = capabilities(result)["objects"]
            owner["active_method_indices"] = [index for index in visible if index in active_source]
            owner["selected_indices"] = [index for index in visible if index in selected_counts]

    def fits(limit: int) -> bool:
        sync_references()
        return measure(result)[0] <= limit

    # First preserve direct experience, then legal actions/catalog, then history
    # and optional structure. A rejected block is removed, never silently sent.
    material_limit = min(working_limit, initial + int((working_limit - initial) * .48))

    def material_priority(item: tuple[int, dict]) -> tuple[bool, int]:
        index, row = item
        info = metadata[row["metadata_ref"]] if "metadata_ref" in row else row
        role = info.get("role", "")
        important = any(label in role for label in ("主体背景", "局部回应", "组织后果", "当前生活"))
        return not important, index

    for index, row in sorted(enumerate(original_materials), key=material_priority):
        projected, undo = add_text(row, material_path, index, 512)
        owner["materials"].append(projected)
        material_page["visible_indices"].append(index)
        material_page["omitted_count"] -= 1
        if not fits(material_limit):
            owner["materials"].pop()
            material_page["visible_indices"].pop()
            material_page["omitted_count"] += 1
            undo_text(undo)
            break
    owner["materials"].sort(key=lambda row: row["material_index"])
    material_page["visible_indices"].sort()
    owner["view"]["material_indices"] = list(material_page["visible_indices"])

    def append_rows(path: str, rows: list, destination: list, limit: int, *, indices_key: str | None = None) -> None:
        descriptor = pages.get(path) or page(path, rows, len(rows), [])
        descriptor.setdefault("visible_indices", [])
        for index, row in enumerate(rows):
            projected = _preview(row)
            if isinstance(projected, dict):
                projected["detail_excerpted"] = projected != row
            destination.append(projected)
            descriptor["visible_indices"].append(index if indices_key is None else row[indices_key])
            descriptor["omitted_count"] -= 1
            if not fits(limit):
                destination.pop()
                descriptor["visible_indices"].pop()
                descriptor["omitted_count"] += 1
                break

    if "reflection" in result:
        append_rows("reflection.scopes", full["reflection"]["scopes"], owner["scopes"], working_limit * .68)
        definition_pool = {row["definition_ref"]: row for row in full["reflection"].get("definition_pool", [])}
        admitted_definitions = set()
        descriptor = pages["reflection.executed_definitions"]
        descriptor["visible_indices"] = []
        for index, definition in enumerate(full["reflection"].get("executed_definitions", [])):
            ref = definition["definition_ref"]
            new_body = ref not in admitted_definitions
            if new_body:
                body = deepcopy(definition_pool[ref])
                body["detail_excerpted"] = False
                owner["definition_pool"].append(body)
            owner["executed_definitions"].append(definition)
            descriptor["visible_indices"].append(index)
            descriptor["omitted_count"] -= 1
            if new_body and not fits(working_limit * .70):
                body = _preview(definition_pool[ref])
                body["detail_excerpted"] = body != definition_pool[ref]
                owner["definition_pool"][-1] = body
            if not fits(working_limit * .70):
                owner["executed_definitions"].pop()
                descriptor["visible_indices"].pop()
                descriptor["omitted_count"] += 1
                if new_body:
                    owner["definition_pool"].pop()
                break
            admitted_definitions.add(ref)
    if "routing_table" in result:
        append_rows("routing_table", full["routing_table"], result["routing_table"], working_limit * .74,
                    indices_key="interface_index")
    if "reading_table" in result:
        # Replace the minimal focus with complete rows when they fit. Interfaces
        # inside a row are themselves paged; every expression retains its index.
        result["reading_table"] = []
        for source_row in full["reading_table"]:
            row = {"focus_index": source_row["focus_index"], "name": str(source_row["name"])[:80], "expressions": []}
            result["reading_table"].append(row)
            if len(result["reading_table"]) > 1 and not fits(working_limit * .68):
                result["reading_table"].pop()
                break
            for expression in source_row["expressions"]:
                row["expressions"].append(_preview(expression))
                if not fits(working_limit * .72):
                    row["expressions"].pop()
                    break
        pages["reading_table"]["visible_indices"] = [row["focus_index"] for row in result["reading_table"]]
        pages["reading_table"]["omitted_count"] = len(full["reading_table"]) - len(result["reading_table"])
    prioritized_objects = sorted([
        {**row, **({"selected_occurrence_count": selected_counts[row["object_index"]]}
                  if row["object_index"] in selected_counts else {})}
        for row in original_objects
    ], key=lambda row: (
        row["object_index"] not in active_source, row["object_index"] not in selected_counts,
        row.get("kind") not in {"effect", "response"}, -row["object_index"],
    ))
    append_rows("reflection.objects", prioritized_objects, owner.get("objects", []), working_limit * .85,
                indices_key="object_index")
    sync_references()
    if "previous_segments" in result:
        # Recent paragraphs get the first page admission, with original indices.
        for index in reversed(range(len(original_history))):
            row, undo = add_text(original_history[index], "previous_segments", index, 1024)
            result["previous_segments"].append(row)
            result["history_table"].append({"history_index": index,
                                            "segment_id": original_history[index]["id"]})
            history_page["visible_indices"].append(index)
            history_page["omitted_count"] -= 1
            if not fits(working_limit * .96):
                result["previous_segments"].pop()
                result["history_table"].pop()
                history_page["visible_indices"].pop()
                history_page["omitted_count"] += 1
                undo_text(undo)
                break
        result["previous_segments"].sort(key=lambda row: row["history_index"])
        result["history_table"].sort(key=lambda row: row["history_index"])
    # Add the complete selected structure if it fits; otherwise an explicit view
    # page is already present. No giant minimum node list survives by obligation.
    previous_view = owner["view"]
    owner["view"] = deepcopy(view_source)
    if not fits(working_limit):
        owner["view"] = previous_view
    else:
        pages["reflection.view" if "reflection" in full else "view"]["omitted_count"] = 0
    actual_stats = result["context_compaction"]
    sync_references()
    actual_stats.update(
        text_entries=len(result["text_pool"]),
        retained_text_chars=sum(sum(end - start for start, end in entry["spans"]) for entry in result["text_pool"]),
        material_occurrences_visible=len(owner["materials"]),
        candidate_objects_visible=len(owner.get("objects", [])),
        history_segments_visible=len(result.get("previous_segments", [])),
    )
    actual_stats["omitted_text_chars"] = actual_stats["original_text_chars"] - actual_stats["retained_text_chars"]
    actual_stats["lossy"] = actual_stats["omitted_text_chars"] > 0
    actual_stats["history_segments_excerpted"] = len(original_history) - sum(
        included_texts[row["text_ref"]]["omitted_chars"] == 0 for row in result.get("previous_segments", []))
    size, shaped = measure(result)
    actual_stats["within_target"] = size <= target_chars
    size, shaped = measure(result)
    # Admissions reserved 1024 bytes for final counters. This invariant concerns
    # every container and schema, independently of narrative length or node count.
    assert size <= hard_limit, "Bounded projection admission invariant violated"
    return result, shaped


def verify_bounded_sources(frame: object, projection: dict) -> bool:
    """Rebuild this version's deterministic view against the exact frozen source.

    The provider projection method performs no network/config-file access. Using
    the same version verifies both source spans and every folded structure page.
    """
    try:
        from opentale.config import Settings
        from opentale.domain import GenerationFrame, ReadingFrame

        from .deepseek import DeepSeekProvider
        frozen = frame.model_dump() if hasattr(frame, "model_dump") else frame
        data = json.loads(next(message["content"] for message in projection["messages"] if message["role"] == "user"))
        manifest = data.get("context_compaction")
        if manifest is None:
            return True
        if manifest.get("algorithm") != ALGORITHM_VERSION:
            return False
        if "candidates" in frozen:
            stage, source = "present", ReadingFrame.model_validate(frozen)
        else:
            stage = "propose" if "routing_table" in data else "realize"
            source = GenerationFrame.model_validate(frozen)
        rebuilt = DeepSeekProvider(Settings(context_target_chars=manifest["target_chars"]),
                                   projection_version=PROJECTION_VERSION).request_projection(source, stage)
        return json.loads(rebuilt["messages"][1]["content"]) == data
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return False
