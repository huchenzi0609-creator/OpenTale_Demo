from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import GenerationFrame, ReadingFrame
from opentale.providers import DeepSeekProvider, OutputFeedback, ProviderError

KEY = "mock-output-boundary-credential"
PROSE = "她想起 café 和 cafe\u0301。\n那句话仍未说完。"


def frames():
    actor = GenerationFrame(
        position_id="p-a", name="留灯人", revision=2, seed_text="她没有熄灯。",
        active_uses=[{"expression_id": "door", "role": "未开的门", "values": ["门外尚有人。"]}],
        condition_fingerprint="not-actor-data", recent_surface=[],
        peers=[{"id": "p-b", "name": "归来的人"}, {"id": "p-c", "name": "等待的人"}],
        peer_interfaces=[
            {"position_id": target, "expression_id": "door", "role": "未开的门"}
            for target in ("p-b", "p-c")
        ],
    )
    reader = ReadingFrame(
        revision=2, start_position_id="p-b", current_focus="p-b",
        candidates=[
            {"position_id": target, "name": "归来的人", "text": "她还没有开门。",
             "active_uses": [{"expression_id": "door", "values": ["屋内灯亮着。"]},
                             {"expression_id": "private-" + target, "values": ["尚未说出的身世。"]}]}
            for target in ("p-a", "p-b")
        ],
        previous_segments=[], open_relations=[],
    )
    return actor, reader


def stage_reply(stage):
    if stage == "propose":
        return {"fragments": [{"text": PROSE, "targets": [
            {"target_position_id": target, "expression_id": "door", "role": "未开的门"}
            for target in ("p-b", "p-c")
        ]}]}
    if stage == "present":
        return {"text": PROSE, "focus_position_id": "p-b", "expression_ids": ["door"], "reinterprets": []}
    return {"text": PROSE}


def envelope(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 73, "completion_tokens": 29}}


def wrap(payload, wrapping):
    raw = json.dumps(payload, ensure_ascii=False)
    return {
        "bom": " \n\ufeff" + raw + " \n",
        "json_fence": "```json\n" + raw + "\n```",
        "bare_fence": "```\r\n" + raw + "\r\n```",
        "json_string": json.dumps(raw, ensure_ascii=False),
        "double_string": json.dumps(json.dumps(raw, ensure_ascii=False), ensure_ascii=False),
        "fenced_string": "```JSON\n" + json.dumps(raw, ensure_ascii=False) + "\n```",
    }[wrapping]


@pytest.mark.parametrize("stage", ["propose", "realize", "present"])
@pytest.mark.parametrize("wrapping", ["bom", "json_fence", "bare_fence", "json_string", "double_string", "fenced_string"])
async def test_complete_syntactic_wrappers_preserve_exact_unicode_and_use_only_one_call(stage, wrapping):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(wrap(stage_reply(stage), wrapping)))

    actor, reader = frames()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await getattr(DeepSeekProvider(Settings(api_key=KEY), client), stage)(
            reader if stage == "present" else actor,
        )
    assert len(requests) == 1
    assert (result.value if stage == "realize" else result.value.text) == PROSE
    assert (result.input_tokens, result.output_tokens) == (73, 29)
    if stage == "propose":
        assert len(result.value.operations) == 2
        assert all(operation.anchor == operation.value == PROSE for operation in result.value.operations)


@pytest.mark.parametrize("raw,category,issue", [
    ('{"text":"one","text":"two"}', "JSON_SYNTAX", "DUPLICATE_FIELD"),
    ('{"text":NaN}', "JSON_SYNTAX", "NON_JSON_NUMBER"),
    ('{"text":1e999}', "JSON_SYNTAX", "NON_JSON_NUMBER"),
    ('before {"text":"她停了一下。"}', "JSON_SYNTAX", "INVALID_JSON"),
    ('{"text":"她停了一下。"} after', "JSON_SYNTAX", "INVALID_JSON"),
    ('{"text":"她停了一下。"}{"text":"然后呢。"}', "JSON_SYNTAX", "INVALID_JSON"),
    ('```json\n{"text":"她停了一下。"}\n```\n说明', "JSON_SYNTAX", "INVALID_JSON"),
    ('[{"text":"她停了一下。"}]', "JSON_TYPE", "ROOT_NOT_OBJECT"),
    ("null", "JSON_TYPE", "ROOT_NOT_OBJECT"),
])
async def test_unsafe_or_ambiguous_shape_is_not_salvaged_and_returns_only_finite_feedback(raw, category, issue):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope(raw)),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key=KEY), client).realize(frames()[0])
    error = caught.value
    assert error.feedback == OutputFeedback(category, (issue,))
    assert error.usage == (73, 29)
    assert error.__context__ is None and error.__cause__ is None
    assert raw not in str(error)
    assert set(error.public()) == {"code", "message", "retryable"}


@pytest.mark.parametrize("stage,reply,issue", [
    ("realize", {}, "text:missing"),
    ("realize", {"text": False}, "text:string_type"),
    ("propose", {"fragments": [{"text": "她停了一下。"}]}, "fragments[].targets:missing"),
    ("present", {"text": "她停了一下。", "expression_ids": ["door"]}, "focus_position_id:missing"),
])
async def test_schema_feedback_reports_fixed_fields_without_union_branches_or_input_values(stage, reply, issue):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope(json.dumps(reply))),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await getattr(DeepSeekProvider(Settings(api_key=KEY), client), stage)(
                frames()[1] if stage == "present" else frames()[0],
            )
    error = caught.value
    assert error.feedback.category == "SCHEMA_VALIDATION" and issue in error.feedback.issues
    assert issue in error.message
    assert "_FragmentProposal" not in error.message and "Proposal." not in error.message
    assert error.usage == (73, 29)


async def test_explicit_repair_only_sends_same_frame_and_safe_feedback_not_rejected_output():
    requests = []
    rejected = {"text": False, "PRIVATE_UNKNOWN_FIELD": "PRIVATE_REJECTED_BIOGRAPHY"}

    def handler(request):
        requests.append(json.loads(request.content))
        reply = rejected if len(requests) == 1 else {"text": PROSE}
        return httpx.Response(200, json=envelope(json.dumps(reply)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key=KEY), client)
        with pytest.raises(ProviderError) as caught:
            await provider.realize(frames()[0])
        # The adapter itself made exactly one billed attempt; retry admission belongs to Runner.
        assert len(requests) == 1
        feedback = caught.value.feedback
        assert "schema:extra_forbidden" in feedback.issues
        result = await provider.realize(frames()[0], repair=feedback)
    assert result.value == PROSE and len(requests) == 2
    assert requests[0]["messages"][1] == requests[1]["messages"][1]
    assert "SCHEMA_VALIDATION" in requests[1]["messages"][0]["content"]
    disclosed = json.dumps(requests[1]["messages"]) + repr(feedback) + str(caught.value)
    assert "PRIVATE_UNKNOWN_FIELD" not in disclosed and "PRIVATE_REJECTED_BIOGRAPHY" not in disclosed
    assert KEY not in disclosed


@pytest.mark.parametrize("mode", ["raw", "escaped", "unknown_field"])
async def test_credential_echo_is_never_eligible_for_format_repair(mode):
    reply = {"text": KEY} if mode != "unknown_field" else {"text": PROSE, KEY: "extra"}
    raw = json.dumps(reply)
    if mode == "escaped":
        raw = raw.replace(KEY, "".join(f"\\u{ord(character):04x}" for character in KEY))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope(raw)),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key=KEY), client).realize(frames()[0])
    assert caught.value.feedback is None
    assert KEY not in str(caught.value) + json.dumps(caught.value.public())
    assert caught.value.__context__ is None


async def test_presentation_schema_and_example_use_only_required_focus_scopes_and_own_history():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(json.dumps(stage_reply("present"))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await DeepSeekProvider(Settings(api_key=KEY), client).present(frames()[1])
    system = requests[0]["messages"][0]["content"]
    schema = json.loads(system.split("JSON schema:\n", 1)[1])
    assert schema["required"] == ["text", "focus_position_id", "expression_ids", "reinterprets"]
    assert len(schema["oneOf"]) == 1
    props = schema["oneOf"][0]["properties"]
    assert props["focus_position_id"] == {"const": "p-b"}
    assert props["expression_ids"]["items"]["enum"] == ["door", "private-p-b"]
    assert props["expression_ids"]["contains"]["enum"] == ["door"]
    assert schema["properties"]["reinterprets"]["maxItems"] == 0
    assert "本阶段完整输出形状示例" in system


@pytest.mark.parametrize("text", ["SCHEMA_VALIDATION", "JSON_SYNTAX", "PROSE_INVALID"])
async def test_repair_diagnostic_codes_never_become_narrative_prose(text):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=envelope(json.dumps({"text": text}))),
    )) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key=KEY), client).realize(frames()[0])
    assert caught.value.feedback.category == "PROSE_INVALID"


@pytest.mark.parametrize("category,issues", [
    ("PRIVATE_VALUE", ()), ("SCHEMA_VALIDATION", ("private-field:missing",)),
    ("SCHEMA_VALIDATION", ("text:PRIVATE_VALUE",)), ("JSON_SYNTAX", ("PRIVATE_VALUE",)),
    ("SCHEMA_VALIDATION", ("text:missing",) * 9),
    ([], ()), ("SCHEMA_VALIDATION", ({},)),
    ("SCHEMA_VALIDATION", ("text." * 50 + "text:missing",)),
])
def test_feedback_constructor_rejects_non_allowlisted_metadata(category, issues):
    with pytest.raises(ValueError, match="Invalid safe output feedback"):
        OutputFeedback(category, issues)
