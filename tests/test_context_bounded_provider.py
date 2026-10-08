"""Finite pages preserve organization priorities and enforce the displayed interfaces."""
from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from opentale.config import Settings
from opentale.providers import DeepSeekProvider, ProviderError
from opentale.providers.context_bounded import verify_bounded_sources
from opentale.providers.feedback import OutputFeedback
from test_context_bounded_runtime import adversarial_frame
from test_reflexive_provider import envelope
from test_reflexive_v2_provider import v2_frame


def data_of(projection):
    return json.loads(projection["messages"][1]["content"])


def directed_frame():
    frame = adversarial_frame("candidate_edges")
    method = {**frame.reflection.objects[1], "id": "method-current-late", "kind": "method",
              "title": "由当前经历理解之前的选择", "inputs": [frame.reflection.objects[0]["id"]],
              "instruction": "让这次得到的消息改变正在进行的关系。", "language_version": 1,
              "order": "listed", "limit": 2}
    frame.reflection.objects.append(method)
    frame.reflection.active_methods = [method["id"]]
    frame.reflection.instruction = method["instruction"]
    frame.reflection.definitions = [{"object_id": method["id"], "operation": "apply",
                                    **{key: method[key] for key in ("title", "inputs", "instruction", "order", "limit",
                                                                   "language_version")}}]
    return frame


def test_active_late_method_instruction_and_definition_get_priority_over_old_catalog():
    frame = directed_frame()
    projection = DeepSeekProvider(Settings()).request_projection(frame, "present")
    data = data_of(projection)
    late_index = len(frame.reflection.objects) - 1
    assert data["reflection"]["instruction"] == frame.reflection.instruction
    assert late_index in data["context_capabilities"]["objects"]
    assert late_index in data["reflection"]["active_method_indices"]
    assert data["reflection"]["executed_definitions"]
    assert data["reflection"]["definition_pool"][0]["instruction"] == frame.reflection.instruction
    assert verify_bounded_sources(frame, projection)
    forged = deepcopy(projection)
    modified = data_of(forged)
    modified["reflection"]["instruction"] = "篡改的组织方向"
    forged["messages"][1]["content"] = json.dumps(modified)
    assert not verify_bounded_sources(frame, forged)


def test_very_large_soft_target_does_not_escape_physical_budget_and_repairs_keep_user():
    frame = adversarial_frame("short_history")
    provider = DeepSeekProvider(Settings(context_target_chars=10**12))
    base = provider.request_projection(frame, "present")
    repaired = provider.request_projection(frame, "present", repair=OutputFeedback("SCHEMA_VALIDATION", ("schema:missing",)))
    assert base["messages"][1] == repaired["messages"][1]
    assert sum(len(message["content"]) for message in repaired["messages"]) <= 512_000
    assert verify_bounded_sources(frame, repaired)


@pytest.mark.parametrize("raw_ids", [False, True])
async def test_history_reference_outside_the_page_cannot_bypass_via_legacy_strings(raw_ids):
    frame = adversarial_frame("short_history")
    provider = DeepSeekProvider(Settings())
    offered = data_of(provider.request_projection(frame, "present"))["context_capabilities"]
    absent = next(index for index in range(len(frame.previous_segments)) if index not in offered["history"])
    focus = offered["focuses"][0]
    reply = {"text": "她收好雨衣，走进厨房。"}
    if raw_ids:
        reply.update(focus_position_id=frame.candidates[focus]["position_id"], expression_ids=[],
                     reinterprets=[frame.previous_segments[absent]["id"]])
    else:
        reply.update(focus_index=focus, expression_indices=[], reinterprets_indices=[absent])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope(reply)),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key="mock-only-key"), client).present(frame)
    assert caught.value.feedback.issues == ("READING_HISTORY_INVALID",)
    assert caught.value.usage == (21, 12)


async def test_hidden_program_reference_is_rejected_against_the_same_page():
    frame = directed_frame()
    offered = data_of(DeepSeekProvider(Settings()).request_projection(frame, "present"))["context_capabilities"]
    absent = next(index for index in range(len(frame.reflection.objects)) if index not in offered["objects"])
    reply = {"text": "她收好雨衣，走进厨房。", "focus_index": offered["focuses"][0],
             "expression_indices": [], "reinterprets_indices": [], "reflection": {"methods": [{
                 "title": "未展示接口", "language_version": 2,
                 "program": {"steps": [{"id": "read", "op": "read", "refs": [{"object_index": absent}]}],
                             "output": "read"}}]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=envelope(reply)),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key="mock-only-key"), client).present(frame)
    assert caught.value.feedback.issues == ("REFLECTION_REFERENCE_INVALID",)


def test_candidate_repetitions_are_losslessly_counted_before_any_excerpt():
    frame = v2_frame(reading=True)
    repeated = {"expression_id": "shared", "role": "当下的关系"}
    frame.candidates[0]["active_uses"] = [repeated] * 12_000
    frame.reflection.materials[0].text = "她已经记住那个约定。"
    data = data_of(DeepSeekProvider(Settings()).request_projection(frame, "present"))
    assert data["candidates"][0]["active_uses"] == [{**repeated, "occurrence_count": 12_000}]
    assert data["context_compaction"]["lossy"] is False
    assert data["context_compaction"]["structure_excerpted"] is False
