"""Offline reading handles compile in exact frozen scopes, never by guessed IDs."""

from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import ReadingFrame
from opentale.providers import DeepSeekProvider, ProviderError
from opentale.providers.feedback import OutputFeedback
from opentale.reflection_types import Material, ReflectionFrame

KEY = "mock-only-reading-credential"
PROSE = "她把回信放进包里，沿河走回家。"


def frame(*, entry: bool = False, reflexive: bool = False) -> ReadingFrame:
    return ReadingFrame(
        revision=7, start_position_id="person-b", current_focus="person-a",
        required_focus_position_id="person-b" if entry else None,
        candidates=[
            {"position_id": f"person-{suffix}", "name": name, "text": "她走到河边。",
             "source_position_id": "private-creator",
             "active_uses": [
                 {"expression_id": "door", "role": "共同经历", "values": ["雨已经停了。"]},
                 {"expression_id": expression, "role": "主体背景", "values": ["她带着回信。"]},
             ]}
            for suffix, name, expression in [("a", "阿青", "a-only"), ("b", "阿白", "private-b")]
        ],
        previous_segments=[] if entry else [
            {"id": "segment-z", "text": "她收下回信。", "reading_id": "private-track"},
            {"id": "segment-a", "text": "她读了回信。", "other_reading_text": "PRIVATE_OTHER_READING"},
        ], open_relations=[],
        reflection=ReflectionFrame(
            objects=[{"id": "object-rain", "kind": "content", "text": "雨已经停了。"}],
            scopes=[{"id": "reading:main", "name": "本条阅读"}],
            materials=[Material(id="object-rain", text="雨已经停了。", role="共同经历")],
            selected_inputs=["object-rain"], groups=[["object-rain"]],
            instruction="沿河边的经历继续。", input_fingerprint="private-fingerprint",
        ) if reflexive else None,
    )


def reply(**changes) -> dict:
    return {"text": PROSE, "focus_index": 1, "expression_indices": [0, 1],
            "reinterprets_indices": [1, 0], **changes}


def envelope(value: dict) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 40}}


async def evaluate(value: dict, reading: ReadingFrame, *, repair=None, capture=None):
    def handler(request):
        if capture is not None:
            capture.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(value))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await DeepSeekProvider(Settings(api_key=KEY), client).present(reading, repair=repair)


async def test_exact_focus_local_expressions_and_chronological_history_compile_without_rebinding():
    captured = []
    result = await evaluate(reply(expression_indices=[1, 0, 1]), frame(), capture=captured)
    assert result.value.text == PROSE
    assert result.value.focus_position_id == "person-b"
    assert result.value.expression_ids == ["private-b", "door", "private-b"]
    assert result.value.reinterprets == ["segment-a", "segment-z"]
    assert (result.input_tokens, result.output_tokens) == (80, 40)
    data = json.loads(captured[0]["messages"][1]["content"])
    assert data["reading_table"][0]["expressions"][0]["expression_id"] == "a-only"
    assert data["reading_table"][1]["expressions"][0]["expression_id"] == "door"
    assert data["history_table"] == [
        {"history_index": 0, "segment_id": "segment-z"},
        {"history_index": 1, "segment_id": "segment-a"},
    ]
    assert "PRIVATE_OTHER_READING" not in json.dumps(data)
    assert "private-creator" not in json.dumps(data) and "private-track" not in json.dumps(data)
    assert KEY not in json.dumps(captured)
    system = captured[0]["messages"][0]["content"]
    schema = json.loads(system.split("JSON schema:\n", 1)[1])
    assert schema["properties"]["reinterprets_indices"]["items"]["enum"] == [0, 1]
    example = json.loads(system.split("本阶段完整输出形状示例", 1)[1].split("\n", 1)[1].split("\n", 1)[0])
    assert set(example) == {"text", "focus_index", "expression_indices", "reinterprets_indices"}


async def test_first_entry_handles_include_only_chosen_start_and_no_foreign_history():
    result = await evaluate(reply(focus_index=0, reinterprets_indices=[]), frame(entry=True))
    assert result.value.focus_position_id == "person-b"
    assert result.value.expression_ids == ["door", "private-b"]
    assert result.value.reinterprets == []


@pytest.mark.parametrize(("changes", "entry", "reason"), [
    ({"focus_index": 2}, False, "READING_FOCUS_INVALID"),
    ({"focus_index": -1}, False, "READING_FOCUS_INVALID"),
    ({"focus_index": 1, "reinterprets_indices": []}, True, "READING_FOCUS_INVALID"),
    ({"expression_indices": [0, 2]}, False, "READING_EXPRESSION_INVALID"),
    ({"expression_indices": [-1]}, False, "READING_EXPRESSION_INVALID"),
    ({"expression_indices": [1]}, False, "READING_SHARED_REQUIRED"),
    ({"reinterprets_indices": [2]}, False, "READING_HISTORY_INVALID"),
    ({"reinterprets_indices": [-1]}, False, "READING_HISTORY_INVALID"),
    ({"focus_index": 0, "reinterprets_indices": [0]}, True, "READING_HISTORY_INVALID"),
])
async def test_out_of_scope_handles_return_only_finite_repair_feedback(changes, entry, reason):
    reading = frame(entry=entry)
    before = reading.model_dump()
    with pytest.raises(ProviderError) as caught:
        await evaluate(reply(**changes), reading)
    error = caught.value
    assert error.code == "MODEL_OUTPUT_INVALID" and error.usage == (80, 40)
    assert error.feedback == OutputFeedback("READING_INVALID", (reason,))
    assert PROSE not in str(error) + json.dumps(error.public())
    assert "person-" not in str(error) + json.dumps(error.public())
    assert reading.model_dump() == before


@pytest.mark.parametrize("changes", [
    {"focus_index": "person-b"}, {"focus_index": True},
    {"expression_indices": ["door"]}, {"reinterprets_indices": [False]},
    {"focus_position_id": "person-b"},
])
async def test_handles_require_integer_types_and_never_mix_named_reference_shapes(changes):
    with pytest.raises(ProviderError) as caught:
        await evaluate(reply(**changes), frame())
    assert caught.value.feedback.category == "SCHEMA_VALIDATION"
    assert "person-b" not in str(caught.value) and "door" not in str(caught.value)


async def test_valid_legacy_reference_shape_is_preserved_exactly():
    result = await evaluate({
        "text": PROSE, "focus_position_id": "person-b", "expression_ids": ["private-b", "door"],
        "reinterprets": ["segment-a", "segment-z"],
    }, frame())
    assert result.value.text == PROSE
    assert result.value.focus_position_id == "person-b"
    assert result.value.expression_ids == ["private-b", "door"]
    assert result.value.reinterprets == ["segment-a", "segment-z"]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"focus_position_id": "阿白"}, "READING_FOCUS_INVALID"),
    ({"expression_ids": ["door", "a-only"]}, "READING_EXPRESSION_INVALID"),
    ({"reinterprets": ["other-reading-segment"]}, "READING_HISTORY_INVALID"),
])
async def test_invalid_legacy_references_are_not_guessed_dropped_or_silently_changed(changes, reason):
    with pytest.raises(ProviderError) as caught:
        await evaluate({
            "text": PROSE, "focus_position_id": "person-b", "expression_ids": ["door"],
            "reinterprets": [], **changes,
        }, frame())
    assert caught.value.feedback == OutputFeedback("READING_INVALID", (reason,))


async def test_reflexive_handles_preserve_empty_expressions_and_exact_selected_methods():
    reading = frame(reflexive=True)
    for candidate in reading.candidates:
        candidate["active_uses"] = []
    captured = []
    result = await evaluate(reply(expression_indices=[], reflection={
        "methods": [{"title": "沿河", "instruction": "沿河边的经历继续。", "object_indices": [0]}],
        "adoptions": [{"scope_index": 0, "draft_index": 0}],
    }), reading, capture=captured)
    assert result.value.focus_position_id == "person-b"
    assert result.value.expression_ids == []
    assert result.value.reflection.methods[0].inputs == ["object-rain"]
    assert result.value.reflection.adoptions[0].scope_id == "reading:main"
    data = json.loads(captured[0]["messages"][1]["content"])
    assert data["reflection"]["materials"] == [{
        "id": "object-rain", "text": "雨已经停了。", "role": "共同经历",
    }]
    assert "private-fingerprint" not in json.dumps(data)


async def test_reading_repair_uses_same_input_and_only_finite_failure_category():
    captured = []
    reading = frame()
    before = reading.model_dump()
    await evaluate(reply(), reading, repair=OutputFeedback(
        "READING_INVALID", ("READING_EXPRESSION_INVALID",),
    ), capture=captured)
    system = captured[0]["messages"][0]["content"]
    assert "READING_INVALID" in system and "READING_EXPRESSION_INVALID" in system
    assert "使用相同的当前输入" in system
    assert reading.model_dump() == before


async def test_transport_cannot_change_scope_or_history_used_by_handle_compiler():
    reading = frame()

    def handler(request):
        reading.candidates[1]["position_id"] = "other-person"
        reading.candidates[1]["active_uses"][1]["expression_id"] = "other-expression"
        reading.previous_segments[1]["id"] = "other-reading-segment"
        return httpx.Response(200, json=envelope(reply()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DeepSeekProvider(Settings(api_key=KEY), client).present(reading)
    assert result.value.focus_position_id == "person-b"
    assert result.value.expression_ids == ["door", "private-b"]
    assert result.value.reinterprets == ["segment-a", "segment-z"]
