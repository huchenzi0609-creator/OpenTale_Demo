"""Deterministic, source-addressed context packing; no model calls or invented summaries.

Material occurrences and the selected view are immutable in this projection. The
text pool shares equal strings; excerpts identify exact ranges in frozen inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy

from opentale.capacity import MAX_MODEL_REQUEST_CHARS

PROJECTION_VERSION = "provider-json-v2-context"
LEGACY_PROJECTION_VERSION = "provider-json-v1"
ALGORITHM_VERSION = "source-excerpts-v1"
REPAIR_RESERVE_CHARS = 4096

CONTEXT_INSTRUCTIONS = """
本次输入使用有出处的文本池压缩。text_ref 指向顶层 text_pool 的 text_index，多个材料
出现位置可以共享同一文本；materials 的顺序、role 和 view 中的材料出现位置继续各自生效。
若材料有 metadata_ref，其 id、role 等字段在顶层 material_metadata 的对应 metadata_index 中。
每项 segments 与 spans 一一对应，都是原文的连续片段；start 包含、end 不包含。
spans 之间有空隙时，中间内容本次省略，分别理解各片段，保留各自的说话者、否定和关系。
partial_sentence=true 表示某个超长单句只能提供字符范围摘录，片段边界仍有未读语境，
只据实际可见文字继续，必要时用 read 取得相邻范围以确认完整含义。
omitted_chars 大于零表示只读到了摘录。依据可见片段继续；需要细节时，可在方法中用
read 的对象引用与 start/end，或 query 找回原对象，原始全文保留在实验中。
候选对象保留本次整数接口、种类和版本；choice/view 的目录是结构概览，当前完整组织
仍在 reflection.view。executed_definitions 的 definition_ref 指向 definition_pool 中
完整的已执行定义；inspection/application 的每次记录仍保留。目录里省略的可选引用
列表与计数分别表示空列表与零；routing_table 保存完整可写接口及其当前角色，可以据它
选择承接目标。目录概览与有损摘录均不
代表已经读过所有历史。近期阅读和被选中的背景、局部回应优先保留，按实际材料继续。
text_ref、spans、text_pool 等属于输入表示。输出继续遵守原 schema，正文和回应直接填写
自然语言 text 字符串；省略压缩说明、来源编号和技术标记。
""".strip()


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compact_schema(value: object) -> object:
    """A contiguous integer enum is exactly the same integer interval."""
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: compact_schema(item) for key, item in value.items()}
    values = result.get("enum")
    if (result.get("type") == "integer" and isinstance(values, list) and values
            and all(type(number) is int for number in values)
            and values == list(range(values[0], values[-1] + 1))):
        result.pop("enum")
        result.update(minimum=values[0], maximum=values[-1])
    return result


def _catalog(reflection: dict) -> None:
    """All capability rows survive; their historical structure is fetched on use."""
    for row in reflection.get("objects", []):
        if row.get("kind") in {"choice", "view"}:
            row["material_preview_count"] = len(row.get("materials", []))
            row["root_group_count"] = len(row.get("groups", row.get("view", {}).get("groups", [])))
            row["structure_compacted"] = True
            for key in ("materials", "view", "group_indices", "offered_indices", "selected_indices"):
                row.pop(key, None)
        elif row.get("kind") == "method" and row.get("program"):
            row["program_step_count"] = len(row["program"].get("steps", []))
            row["definition_compacted"] = True
            row.pop("program")
        text = row.get("text")
        if isinstance(text, str) and len(text) > 160:
            # This remains a declared candidate preview, never an executed material.
            row["text"] = text[:160]
            row["preview_truncated"] = True
        for key in list(row):
            if ((key.endswith("_count") and row[key] == 0)
                    or (key.endswith("_indices") and row[key] == [])
                    or (key == "preview_truncated" and row[key] is False)):
                row.pop(key)
    if reflection.get("view") is not None:
        reflection.pop("groups", None)  # Same selected structure is already in view.
    definitions, pool, known = [], [], {}
    for source in reflection.get("executed_definitions", []):
        body = {key: value for key, value in source.items()
                if key not in {"definition_index", "object_index", "operation", "transient_definition"}}
        signature = compact_json(body)
        if signature not in known:
            known[signature] = len(pool)
            pool.append({"definition_ref": len(pool), **body})
        definitions.append({key: value for key, value in source.items()
                            if key in {"definition_index", "object_index", "operation", "transient_definition"}}
                           | {"definition_ref": known[signature]})
    reflection["executed_definitions"] = definitions
    reflection["definition_pool"] = pool


def _units(text: str) -> list[tuple[int, int]]:
    # A unit retains a complete sentence or paragraph, including its attribution.
    spans = [(match.start(), match.end()) for match in re.finditer(
        r".+?(?:[。！？!?][”’\"」』]*|\n+|$)", text, flags=re.DOTALL,
    )]
    return spans or ([(0, len(text))] if text else [])


def _excerpt(text: str, allowance: int, terms: list[str]) -> list[tuple[int, int]]:
    if len(text) <= allowance:
        return [(0, len(text))] if text else []
    units = _units(text)
    if not units:
        return []
    selected = {0, len(units) - 1}
    used = sum(units[index][1] - units[index][0] for index in selected)
    if used > allowance and any(units[index][1] - units[index][0] > max(4096, allowance * 2)
                                for index in selected):
        head = max(1, allowance // 2)
        tail = max(1, allowance - head)
        return [(0, head), (len(text) - tail, len(text))]
    ranked = sorted(range(1, len(units) - 1), key=lambda index: (
        -sum(term in text[units[index][0]:units[index][1]] for term in terms), index,
    ))
    for index in ranked:
        length = units[index][1] - units[index][0]
        if used + length <= allowance:
            selected.add(index)
            used += length
    merged: list[tuple[int, int]] = []
    for index in sorted(selected):
        start, end = units[index]
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _allowances(lengths: list[int], weights: list[float], budget: int) -> list[int]:
    # Every distinct source gets balanced head/tail coverage. The target is soft:
    # a whole sentence may exceed its allowance; physical transport remains checked.
    result = [min(length, 96) for length in lengths]
    available = max(0, budget - sum(result))
    pending = {index for index, length in enumerate(lengths) if result[index] < length}
    while available and pending:
        total = sum(weights[index] for index in pending)
        allocated = 0
        for index in sorted(pending):
            delta = min(lengths[index] - result[index], max(1, int(available * weights[index] / total)))
            result[index] += delta
            allocated += delta
        available = max(0, available - allocated)
        pending = {index for index in pending if result[index] < lengths[index]}
    return result


def compact_context(data: dict, *, system_chars: int, input_chars: int, target_chars: int) -> dict:
    """Pack user input once. Repair text never affects this deterministic result."""
    result = deepcopy(data)
    if "peer_interfaces" in result and "routing_table" in result:
        routed = {(row["target_position_id"], row["expression_id"], role)
                  for row in result["routing_table"] for role in row.get("current_roles", [])}
        if all((row["position_id"], row["expression_id"], row["role"]) in routed
               for row in result["peer_interfaces"]):
            result.pop("peer_interfaces")
    reflection = result.get("reflection")
    if isinstance(reflection, dict):
        _catalog(reflection)
    elif result.get("view") is not None:
        result.pop("groups", None)
    pool: list[dict] = []
    originals: list[str] = []
    weights: list[float] = []
    known: dict[str, int] = {}
    recent: set[int] = set()
    history = result.get("previous_segments", [])
    history_count = len(history)

    def intern(row: dict, path: str, index: int, *, weight: float = 1, is_recent: bool = False) -> None:
        text = row.pop("text", None)
        if not isinstance(text, str):
            return
        identity = known.get(text)
        if identity is None:
            identity = len(pool)
            known[text] = identity
            originals.append(text)
            weights.append(weight)
            pool.append({"text_index": identity, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                         "original_chars": len(text), "spans": [[0, len(text)]] if text else [],
                         "segments": [text] if text else [], "omitted_chars": 0,
                         "partial_sentence": False, "sources": []})
        else:
            weights[identity] = max(weights[identity], weight)
        row["text_ref"] = identity
        pool[identity]["sources"].append({"path": path, "index": index})
        if is_recent:
            recent.add(identity)

    material_owner = reflection if isinstance(reflection, dict) else result
    materials = material_owner.get("materials", [])
    material_path = "reflection.materials" if isinstance(reflection, dict) else "materials"
    for index, row in enumerate(materials):
        role = row.get("role", "")
        weight = 3 if any(term in role for term in ("主体背景", "局部回应", "组织后果")) else 1
        intern(row, material_path, index, weight=weight)
    for index, row in enumerate(history):
        intern(row, "previous_segments", index, weight=6 if index >= history_count - 2 else 1,
               is_recent=index >= history_count - 2)
    metadata, metadata_indices, packed_materials = [], {}, []
    for row in materials:
        body = {key: value for key, value in row.items() if key != "text_ref"}
        signature = compact_json(body)
        if signature not in metadata_indices:
            metadata_indices[signature] = len(metadata)
            metadata.append({"metadata_index": len(metadata), **body})
        packed_materials.append({"metadata_ref": metadata_indices[signature], "text_ref": row["text_ref"]})
    if len(compact_json(packed_materials)) + len(compact_json(metadata)) + 32 < len(compact_json(materials)):
        material_owner["materials"] = packed_materials
        result["material_metadata"] = metadata
    result["text_pool"] = pool
    manifest = {
        "algorithm": ALGORITHM_VERSION, "projection_version": PROJECTION_VERSION,
        "target_chars": target_chars, "input_chars": input_chars, "output_chars": 0,
        "lossy": False, "text_entries": len(pool), "material_occurrences": len(materials),
        "candidate_objects": len(reflection.get("objects", [])) if isinstance(reflection, dict) else 0,
        "original_text_chars": sum(map(len, originals)), "retained_text_chars": sum(map(len, originals)),
        "omitted_text_chars": 0, "history_segments": history_count, "history_segments_excerpted": 0,
        "within_target": True,
    }
    result["context_compaction"] = manifest

    def measure() -> int:
        for _ in range(4):
            size = system_chars + len(compact_json(result))
            if size == manifest["output_chars"]:
                return size
            manifest["output_chars"] = size
        return manifest["output_chars"]

    # Lossless text sharing is always attempted before source-backed excerpts.
    size = measure()
    if size > target_chars:
        guide = str(material_owner.get("instruction", "")) + str(result.get("current_focus", ""))
        terms = [term for term, _ in Counter(re.findall(r"[\u4e00-\u9fff]{2}|[A-Za-z]{3,}", guide)).most_common(24)]
        lengths = list(map(len, originals))
        # Omit text only to estimate representation overhead; no final empty summaries are emitted.
        overhead = size - sum(lengths)
        effective_target = target_chars
        if overhead + sum(min(length, 96) for length in lengths) + 1024 > target_chars:
            # A soft target below mandatory interface size must not erase the
            # substance of every source. Keep lossless text if physically possible;
            # otherwise spend the actual transport capacity on balanced excerpts.
            effective_target = MAX_MODEL_REQUEST_CHARS - REPAIR_RESERVE_CHARS
            if size <= effective_target:
                manifest["within_target"] = False
                measure()
                return result
        budget = max(len(pool) * 96, effective_target - overhead - 1024)
        for _ in range(4):
            allowances = _allowances(lengths, weights, budget)
            # Recent reading remains whole when that fits within its priority share.
            for index in recent:
                if lengths[index] <= max(allowances[index], budget // 4):
                    allowances[index] = lengths[index]
            for index, (entry, text) in enumerate(zip(pool, originals, strict=True)):
                spans = _excerpt(text, allowances[index], terms)
                entry["spans"] = [list(span) for span in spans]
                entry["segments"] = [text[start:end] for start, end in spans]
                entry["omitted_chars"] = len(text) - sum(end - start for start, end in spans)
                units = _units(text)
                starts, ends = {start for start, _ in units}, {end for _, end in units}
                entry["partial_sentence"] = any(start not in starts or end not in ends for start, end in spans)
            manifest["omitted_text_chars"] = sum(entry["omitted_chars"] for entry in pool)
            manifest["retained_text_chars"] = sum(lengths) - manifest["omitted_text_chars"]
            manifest["lossy"] = manifest["omitted_text_chars"] > 0
            manifest["history_segments_excerpted"] = sum(
                pool[row["text_ref"]]["omitted_chars"] > 0 for row in history if "text_ref" in row
            )
            size = measure()
            if size <= effective_target or budget <= len(pool) * 96:
                break
            budget = max(len(pool) * 96, budget - (size - effective_target) - 256)
    manifest["within_target"] = size <= target_chars
    measure()
    return result


def verify_compaction_sources(frame: object, projection: dict) -> bool:
    """Validate excerpts against frozen inputs, without reading storage or a model."""
    try:
        frozen = frame.model_dump() if hasattr(frame, "model_dump") else frame
        data = json.loads(next(message["content"] for message in projection["messages"]
                               if message["role"] == "user"))
        manifest = data.get("context_compaction")
        if manifest is None:
            return True
        if manifest.get("algorithm") != ALGORITHM_VERSION or not isinstance(frozen, dict):
            return False
        original_materials = (frozen.get("reflection") or {}).get("materials", [])
        originals = {"materials": original_materials, "reflection.materials": original_materials,
                     "previous_segments": frozen.get("previous_segments", [])}
        current = {"materials": data.get("materials", []),
                   "reflection.materials": data.get("reflection", {}).get("materials", []),
                   "previous_segments": data.get("previous_segments", [])}
        for path, rows in current.items():
            allowed = {"id", "position_id", "expression_id", "role"} if path != "previous_segments" else {
                "id", "revision", "focus_position_id", "expression_ids", "reinterprets",
            }
            for index, row in enumerate(rows):
                expected_metadata = {key: value for key, value in originals[path][index].items()
                                     if key in allowed and (path == "previous_segments" or value is not None)}
                actual_metadata = {key: value for key, value in row.items() if key != "text_ref"}
                if "metadata_ref" in actual_metadata:
                    metadata_index = actual_metadata["metadata_ref"]
                    if type(metadata_index) is not int or metadata_index < 0 or set(actual_metadata) != {"metadata_ref"}:
                        return False
                    metadata = data["material_metadata"][metadata_index]
                    if metadata["metadata_index"] != metadata_index:
                        return False
                    actual_metadata = {key: value for key, value in metadata.items() if key != "metadata_index"}
                if actual_metadata != expected_metadata:
                    return False
        seen = set()
        retained, omitted, total = 0, 0, 0
        pool = data["text_pool"]
        for index, entry in enumerate(pool):
            if entry["text_index"] != index or not entry["sources"]:
                return False
            text = None
            for source in entry["sources"]:
                path, ordinal = source["path"], source["index"]
                if path not in originals or type(ordinal) is not int or ordinal < 0:
                    return False
                key = (path, ordinal)
                value = originals[path][ordinal]["text"]
                if key in seen or current[path][ordinal].get("text_ref") != index:
                    return False
                seen.add(key)
                if text is not None and value != text:
                    return False
                text = value
            if (entry["original_chars"] != len(text)
                    or entry["sha256"] != hashlib.sha256(text.encode()).hexdigest()
                    or len(entry["spans"]) != len(entry["segments"])):
                return False
            previous, kept = 0, 0
            for span, segment in zip(entry["spans"], entry["segments"], strict=True):
                start, end = span
                if (type(start) is not int or type(end) is not int or start < previous
                        or end < start or end > len(text) or text[start:end] != segment):
                    return False
                previous, kept = end, kept + end - start
            if entry["omitted_chars"] != len(text) - kept:
                return False
            retained += kept
            omitted += entry["omitted_chars"]
            total += len(text)
        expected = {(path, index) for path, rows in current.items() for index, row in enumerate(rows)}
        history = current["previous_segments"]
        material_count = len(current["materials"]) + len(current["reflection.materials"])
        return (seen == expected and material_count == len(original_materials)
                and len(history) == len(originals["previous_segments"])
                and manifest["text_entries"] == len(pool) and manifest["material_occurrences"] == material_count
                and manifest["original_text_chars"] == total and manifest["retained_text_chars"] == retained
                and manifest["omitted_text_chars"] == omitted and manifest["lossy"] == (omitted > 0)
                and manifest["history_segments"] == len(history)
                and manifest["history_segments_excerpted"] == sum(
                    pool[row["text_ref"]]["omitted_chars"] > 0 for row in history))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return False
