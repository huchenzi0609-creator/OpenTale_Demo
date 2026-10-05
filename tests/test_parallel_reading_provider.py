from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import ReadingFrame
from opentale.providers import DeepSeekProvider, FixtureProvider, ProviderError


def reading_frame(start: str | None = "person-b") -> ReadingFrame:
    return ReadingFrame(
        revision=7,
        start_position_id=start,
        required_focus_position_id=start,
        current_focus=start,
        candidates=[
            {
                "position_id": f"person-{suffix}", "name": name, "text": text,
                "active_uses": [
                    {"expression_id": "door", "role": "未开的门", "values": [value]},
                    {"expression_id": f"private-{suffix}", "role": "主体背景", "values": ["私密经历。"]},
                ],
            }
            for suffix, name, text, value in [
                ("a", "留灯人", "她留着灯，却没再等敲门声。", "门外有人已经走了。"),
                ("b", "归来的人", "她在门外听见房间里还有人。", "里面的人并没有睡。"),
                ("c", "屋里的人", "她听着灯丝响，忽然认出了那阵脚步。", "门外的人像是自己。"),
            ]
        ],
        previous_segments=[],
        open_relations=[{
            "position_id": "person-c", "position_ids": ["person-a", "person-b", "person-c"],
            "expression_id": "door", "role": "未开的门",
        }],
    )


async def test_fixture_parallel_entries_share_live_world_but_begin_at_selected_views():
    first = reading_frame("person-a")
    second = reading_frame("person-b")
    fixture = FixtureProvider()
    entry_a = (await fixture.present(first)).value
    entry_b = (await fixture.present(second)).value
    assert entry_a.focus_position_id == "person-a"
    assert entry_b.focus_position_id == "person-b"
    assert entry_a.expression_ids == entry_b.expression_ids == ["door"]
    assert "门外有人已经走了。" in entry_a.text
    assert "里面的人并没有睡。" in entry_b.text
    assert entry_a.text != entry_b.text
    assert "私密经历" not in entry_a.text + entry_b.text


async def test_fixture_entry_reads_updated_values_instead_of_only_relabeling_focus():
    frame = reading_frame()
    fixture = FixtureProvider()
    before = (await fixture.present(frame)).value
    changed = frame.model_copy(deep=True)
    changed.candidates[1]["active_uses"][0]["values"] = ["房间已经空了，灯却没有熄。"]
    after = (await fixture.present(changed)).value
    assert before.focus_position_id == after.focus_position_id == "person-b"
    assert before.text != after.text
    assert "房间已经空了" in after.text and "里面的人并没有睡" not in after.text


async def test_fixture_after_entry_transfers_focus_using_only_this_reading_history():
    frame = reading_frame()
    frame.required_focus_position_id = None
    frame.previous_segments = [{
        "id": "segment-own", "focus_position_id": "person-b", "expression_ids": ["door"],
        "text": "她曾以为屋里没有人。",
    }]
    presentation = (await FixtureProvider().present(frame)).value
    assert presentation.focus_position_id == "person-c"
    assert presentation.reinterprets == ["segment-own"]
    assert "segment-own" not in presentation.text


async def test_deepseek_projection_has_entry_scopes_and_own_history_without_track_metadata():
    frame = reading_frame()
    frame.required_focus_position_id = None
    frame.previous_segments = [{
        "id": "own-segment", "text": "她一直以为那间屋子是空的。", "expression_ids": ["door"],
        "reading_id": "private-reading-handle", "other_reading_text": "OTHER_READING_PROSE",
    }]
    frame.candidates[0]["reading_id"] = "private-reading-handle"
    frame.candidates[0]["source_position_id"] = "hidden-world-creator"
    frame.open_relations[0]["other_reading_text"] = "OTHER_READING_PROSE"
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "text": "她在灯下停住，门外的人却使先前的空屋不再像空屋。",
                "focus_position_id": "person-c", "expression_ids": ["door"],
                "reinterprets": ["own-segment"],
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 40},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = (await DeepSeekProvider(Settings(api_key="mock-only-credential"), client=client).present(frame)).value
    assert value.focus_position_id == "person-c"
    assert value.reinterprets == ["own-segment"]
    data = json.loads(captured[0]["messages"][1]["content"])
    assert data["start_position_id"] == "person-b"
    assert data["required_focus_position_id"] is None
    assert data["shared_expression_ids"] == ["door"]
    assert data["previous_segments"][0]["text"] == frame.previous_segments[0]["text"]
    actor_input = json.dumps(data)
    assert "OTHER_READING_PROSE" not in actor_input
    assert "private-reading-handle" not in actor_input and "hidden-world-creator" not in actor_input
    system = captured[0]["messages"][0]["content"]
    assert "这条阅读" in system and "实际影响" in system


@pytest.mark.parametrize("violation", ["wrong_start", "other_scope", "private_only", "other_history"])
async def test_deepseek_invalid_reading_references_retain_known_usage(violation):
    frame = reading_frame()
    frame.candidates[2]["active_uses"].append({"expression_id": "other-scope", "values": ["另一处才有的经历。"]})
    reply = {
        "text": "她在门外停住，知道灯仍在屋内。", "focus_position_id": "person-b",
        "expression_ids": ["door"], "reinterprets": [],
    }
    if violation == "wrong_start":
        reply["focus_position_id"] = "person-c"
    elif violation == "other_scope":
        reply["expression_ids"].append("other-scope")
    elif violation == "private_only":
        reply["expression_ids"] = ["private-b"]
    else:
        reply["reinterprets"] = ["another-reading-segment"]

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key="mock-only-credential"), client=client)
        with pytest.raises(ProviderError) as caught:
            await provider.present(frame)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert caught.value.usage == (120, 60)
    assert "another-reading-segment" not in json.dumps(caught.value.public())


async def test_deepseek_rejects_unavailable_start_before_dispatch():
    dispatched = []

    def handler(request):
        dispatched.append(True)
        raise AssertionError("No request may be sent for an unavailable entry")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key="mock-only-credential"), client=client)
        with pytest.raises(ProviderError) as caught:
            await provider.present(reading_frame("missing-person"))
    assert caught.value.code == "READING_FRAME_INVALID"
    assert caught.value.usage is None
    assert not dispatched
