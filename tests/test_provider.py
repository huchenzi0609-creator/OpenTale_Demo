from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import GenerationFrame, ReadingFrame
from opentale.providers import DeepSeekProvider, FixtureProvider, ProviderError, make_provider
from opentale.providers.deepseek import MAX_RESPONSE_BYTES, _proposal_schema, _shared_interface
from pydantic import BaseModel

TEST_CREDENTIAL = "unit-test-only-credential"


def generation_frame(position_id="p-a", name="折信人", revision=0):
    return GenerationFrame(
        position_id=position_id,
        name=name,
        revision=revision,
        seed_text="她停在那句话前，还没有决定怎样继续。",
        condition_fingerprint="diagnostic-fingerprint-must-not-enter-prompt",
        active_uses=[{
            "id": "use-letter", "expression_id": "e-letter", "role": "共同生活",
            "text": "给尚未出生的你。", "values": ["她记得尚未寄出的信。", "椅子上没有人。"],
            "source_position_id": "hidden-creator", "source_expression_id": "hidden-origin",
        }],
        peers=[{"id": "p-b", "name": "写信人"}, {"id": "p-c", "name": "读信人"}],
        recent_surface=[],
    )


def response(content, *, finish_reason="stop", reasoning="hidden-reasoning-must-not-persist"):
    return {
        "choices": [{"message": {"content": content, "reasoning_content": reasoning}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 40},
    }


def provider_with_handler(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeepSeekProvider(Settings(api_key=TEST_CREDENTIAL), client=client), client


async def test_fixture_operations_are_text_anchored_and_target_peers():
    frame = generation_frame()
    result = await FixtureProvider().propose(frame)
    assert len(result.value.operations) == 2
    assert {op.target_position_id for op in result.value.operations} == {"p-b", "p-c"}
    assert len({op.role for op in result.value.operations}) == 1
    assert all(op.anchor in result.value.text and op.value == op.anchor for op in result.value.operations)
    assert all(op.expression_id == "e-letter" for op in result.value.operations)


async def test_fixture_realization_actually_reads_every_binding_and_changes_after_ablation():
    fixture = FixtureProvider()
    frame = generation_frame()
    baseline = (await fixture.realize(frame)).value
    assert "她记得尚未寄出的信。" in baseline and "椅子上没有人。" in baseline
    detached = frame.model_copy(deep=True)
    detached.active_uses[0]["values"] = ["椅子上没有人。"]
    changed = (await fixture.realize(detached)).value
    assert changed != baseline
    assert "她记得尚未寄出的信。" not in changed
    assert frame.active_uses[0]["values"] == ["她记得尚未寄出的信。", "椅子上没有人。"]


async def test_fixture_present_selects_live_text_and_reinterprets_shared_previous_segment():
    reading = ReadingFrame(
        revision=1,
        candidates=[{"position_id": "p-a", "name": "折信人", "text": "她把信折好。",
                     "active_uses": [{"expression_id": "e-letter"}]}],
        previous_segments=[{"id": "segment-old", "text": "她曾经展开信。", "expression_ids": ["e-letter"]}],
        open_relations=[],
    )
    value = (await FixtureProvider().present(reading)).value
    assert "她把信折好。" in value.text
    assert value.expression_ids == ["e-letter"]
    assert value.reinterprets == ["segment-old"]
    assert "segment-old" not in value.text


async def test_fixture_reading_selection_changes_with_focus_and_relations_at_same_revision():
    candidates = [
        {"position_id": f"p-{suffix}", "name": suffix, "text": f"{suffix}停在灯下。",
         "active_uses": [{"expression_id": "e-letter"}]}
        for suffix in "abc"
    ]
    fixture = FixtureProvider()
    frame = ReadingFrame(revision=2, candidates=candidates, previous_segments=[],
                         current_focus="p-a", open_relations=[
                             {"position_id": "p-b", "expression_id": "e-letter", "sources": ["p-a", "p-c"]}
                         ])
    baseline = (await fixture.present(frame)).value
    assert baseline.focus_position_id == "p-b"
    different_relation = frame.model_copy(deep=True)
    different_relation.open_relations[0]["position_id"] = "p-c"
    shifted = (await fixture.present(different_relation)).value
    assert shifted.focus_position_id == "p-c" and shifted.text != baseline.text
    no_relations = frame.model_copy(update={"open_relations": []})
    first_focus = (await fixture.present(no_relations)).value.focus_position_id
    other_focus = no_relations.model_copy(update={"current_focus": first_focus})
    assert (await fixture.present(other_focus)).value.focus_position_id != first_focus


async def test_corridor_fixture_preserves_carrier_instead_of_inventing_letter():
    frame = generation_frame()
    frame.name = "林栖"
    frame.peers = [{"id": "p-b", "name": "陈默"}, {"id": "p-c", "name": "小岚"}]
    frame.active_uses[0]["text"] = "门外响过一次脚步声。她把灯留着，没有开门。"
    frame.active_uses[0]["values"] = [frame.active_uses[0]["text"]]
    fixture = FixtureProvider()
    proposal = (await fixture.propose(frame)).value
    realization = (await fixture.realize(frame)).value
    assert "门" in proposal.text and "灯" in realization
    assert "信" not in proposal.text and "信" not in realization


async def test_request_disables_thinking_and_excludes_technical_creator_provenance():
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.headers["authorization"] == f"Bearer {TEST_CREDENTIAL}"
        return httpx.Response(200, json=response(json.dumps({"text": "她重新读起那封信。"})))

    provider, client = provider_with_handler(handler)
    try:
        result = await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert result.value == "她重新读起那封信。"
    assert (result.input_tokens, result.output_tokens) == (80, 40)
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["response_format"] == {"type": "json_object"}
    actor_input = captured[0]["messages"][1]["content"]
    assert "hidden-creator" not in actor_input and "hidden-origin" not in actor_input
    assert "diagnostic-fingerprint" not in actor_input
    assert TEST_CREDENTIAL not in actor_input
    assert "hidden-reasoning" not in repr(result)


async def test_reading_provider_input_keeps_scopes_but_excludes_creator_provenance():
    reading = ReadingFrame(
        revision=1,
        candidates=[{"position_id": "p-a", "name": "折信人", "text": "她把信折好。",
                     "active_uses": [{"expression_id": "e-letter", "role": "共同生活", "values": ["她在灯下。"],
                                      "source_position_id": "hidden-creator"}]}],
        previous_segments=[{"id": "segment-old", "text": "她曾经展开信。", "expression_ids": ["e-letter"],
                            "source_position_id": "hidden-creator"}],
        open_relations=[{"position_id": "p-a", "expression_id": "e-letter", "role": "共同生活",
                         "sources": ["hidden-creator"], "use_id": "diagnostic-handle"}],
    )

    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["open_relations"][0]["position_id"] == "p-a"
        actor_input = json.dumps(data)
        assert "hidden-creator" not in actor_input and "diagnostic-handle" not in actor_input
        assert "source_position_id" not in actor_input
        return httpx.Response(200, json=response(json.dumps({
            "text": "她把信折好，灯却仍然留着。", "focus_position_id": "p-a",
            "expression_ids": ["e-letter"], "reinterprets": ["segment-old"],
        })))

    provider, client = provider_with_handler(handler)
    try:
        value = (await provider.present(reading)).value
    finally:
        await client.aclose()
    assert value.reinterprets == ["segment-old"]


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (400, False), (429, True), (503, True), (302, False)])
async def test_http_errors_are_safe_and_never_expose_response_body(status, retryable, caplog):
    def handler(request):
        return httpx.Response(status, text=f"RAW_PRIVATE_BODY {TEST_CREDENTIAL} secret-prompt", headers={"location": "https://evil.test"})

    provider, client = provider_with_handler(handler)
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.retryable is retryable
    assert caught.value.usage is None
    exposed = json.dumps(caught.value.public()) + str(caught.value) + caplog.text
    assert TEST_CREDENTIAL not in exposed and "RAW_PRIVATE_BODY" not in exposed
    assert "secret-prompt" not in exposed and "evil.test" not in exposed


async def test_timeout_is_retryable_without_exposing_exception_request():
    def handler(request):
        raise httpx.ReadTimeout(f"secret {TEST_CREDENTIAL}", request=request)

    provider, client = provider_with_handler(handler)
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "PROVIDER_TIMEOUT"
    assert caught.value.retryable
    assert caught.value.usage is None
    assert TEST_CREDENTIAL not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "content",
    ["not json", '{"text":"first","text":"second"}', '{"text":false}', '{"text":""}',
     '{"text":"ok","extra":"private"}', '```json\n{"text":"ok"}\n```',
     '{"text":"CoT 参考233"}', '{"text":"e-letter 技术引用"}',
     json.dumps({"text": TEST_CREDENTIAL}), '{"text":NaN}'],
)
async def test_invalid_json_schema_or_prose_is_bounded_safe_failure(content):
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert caught.value.retryable
    assert TEST_CREDENTIAL not in str(caught.value)
    assert "private" not in str(caught.value)


async def test_escaped_credential_in_surface_is_rejected_after_json_decoding():
    escaped = "".join(f"\\u{ord(character):04x}" for character in TEST_CREDENTIAL)
    content = '{"text":"她看见了' + escaped + '，然后停下。"}'
    assert TEST_CREDENTIAL not in content
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert TEST_CREDENTIAL not in str(caught.value)
    assert TEST_CREDENTIAL not in json.dumps(caught.value.public())


@pytest.mark.parametrize("field", ["text", "role", "value", "anchor", "target_position_id"])
async def test_escaped_credential_in_nested_proposal_fields_never_becomes_model_result(field):
    payload = {
        "position_id": "p-a", "based_on_revision": 0,
        "text": "写信人正在写第一句。",
        "operations": [{"op": "rebind", "target_position_id": "p-b", "expression_id": "e-letter",
                        "role": "共同生活", "value": "写信人正在写第一句。", "anchor": "正在写第一句"}],
    }
    if field == "text":
        payload["text"] = TEST_CREDENTIAL
    else:
        payload["operations"][0][field] = TEST_CREDENTIAL
    escaped = "".join(f"\\u{ord(character):04x}" for character in TEST_CREDENTIAL)
    content = json.dumps(payload, ensure_ascii=False).replace(TEST_CREDENTIAL, escaped)
    assert TEST_CREDENTIAL not in content
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.propose(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert TEST_CREDENTIAL not in str(caught.value)
    assert TEST_CREDENTIAL not in json.dumps(caught.value.public())


async def test_escaped_credential_in_decoded_mapping_key_is_rejected_at_provider_boundary():
    class MappingPayload(BaseModel):
        mapping: dict[str, str]

    escaped = "".join(f"\\u{ord(character):04x}" for character in TEST_CREDENTIAL)
    content = '{"mapping":{"' + escaped + '":"ordinary value"}}'
    assert TEST_CREDENTIAL not in content
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider._request("只输出 json。", {}, MappingPayload)
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert TEST_CREDENTIAL not in str(caught.value)
    assert TEST_CREDENTIAL not in json.dumps(caught.value.public())


async def test_truncation_is_distinguished_from_schema_error():
    provider, client = provider_with_handler(
        lambda request: httpx.Response(200, json=response('{"text":"cut', finish_reason="length"))
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_TRUNCATED"
    assert caught.value.usage == (80, 40)


async def test_response_size_limit_applies_before_decoding():
    provider, client = provider_with_handler(
        lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_TOO_LARGE"


async def test_missing_key_makes_no_network_request():
    def forbidden(request):
        pytest.fail("An unconfigured provider must not access network")

    client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    try:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(), client).realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "API_KEY_MISSING"


@pytest.mark.parametrize("invalid", ["anchor", "self", "revision", "expression"])
async def test_proposals_validate_live_targets_revision_expression_and_exact_anchor(invalid):
    payload = {
        "position_id": "p-a", "based_on_revision": 0, "text": "写信人正在写第一句。",
        "operations": [{"op": "rebind", "target_position_id": "p-b", "expression_id": "e-letter",
                        "role": "共同生活", "value": "写信人正在写第一句。", "anchor": "正在写第一句"}],
    }
    if invalid == "anchor":
        payload["operations"][0]["anchor"] = "不存在的原文"
    elif invalid == "self":
        payload["operations"][0]["target_position_id"] = "p-a"
    elif invalid == "revision":
        payload["based_on_revision"] = 1
    else:
        payload["operations"][0]["expression_id"] = "new-expression"
    provider, client = provider_with_handler(
        lambda request: httpx.Response(200, json=response(json.dumps(payload, ensure_ascii=False)))
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.propose(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


async def test_peer_background_interface_can_be_rebound_without_exposing_private_seed_or_provenance():
    frame = generation_frame()
    frame.peer_interfaces = [{
        "position_id": "p-b", "expression_id": "seed-p-b", "role": "主体背景",
        "text": "private-biography-must-not-enter-request", "source_position_id": "hidden-creator",
    }]
    payload = {
        "position_id": "p-a", "based_on_revision": 0,
        "text": "写信人把那段童年当作尚未寄出的生活。",
        "operations": [{"op": "rebind", "target_position_id": "p-b", "expression_id": "seed-p-b",
                        "role": "主体背景", "value": "写信人把那段童年当作尚未寄出的生活。",
                        "anchor": "那段童年当作尚未寄出的生活"}],
    }

    def handler(request):
        actor = json.loads(request.content)["messages"][1]["content"]
        assert "private-biography-must-not-enter-request" not in actor
        assert "hidden-creator" not in actor
        assert "seed-p-b" in actor
        return httpx.Response(200, json=response(json.dumps(payload, ensure_ascii=False)))

    provider, client = provider_with_handler(handler)
    try:
        result = await provider.propose(frame)
    finally:
        await client.aclose()
    assert result.value.operations[0].expression_id == "seed-p-b"


def test_factory_has_explicit_modes_and_safe_unknown_mode():
    assert isinstance(make_provider("fixture", Settings()), FixtureProvider)
    assert isinstance(make_provider("deepseek", Settings()), DeepSeekProvider)
    with pytest.raises(ProviderError) as caught:
        make_provider(TEST_CREDENTIAL, Settings())
    assert TEST_CREDENTIAL not in str(caught.value)


def fragment_reply(text="写信人正在写第一句。", *, targets=None):
    if targets is None:
        targets = [
            {"target_position_id": target, "expression_id": "e-letter", "role": "共同生活"}
            for target in ("p-b", "p-c")
        ]
    return {"fragments": [{"text": text, "targets": targets}]}


async def test_fragment_proposal_compiles_exact_unicode_prose_once_without_changing_runtime_contract():
    text = "写信人想起了 café 与 cafe\u0301。\n读信人没有把「……」说完。"
    payload = fragment_reply(text)
    payload["fragments"].insert(0, {"text": "折信人暂且停在灯下。", "targets": []})
    payload["fragments"][1]["targets"][1]["role"] = "尚未说出的约定"
    captured = []

    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json=response(json.dumps(payload, ensure_ascii=False)))

    provider, client = provider_with_handler(handler)
    try:
        result = await provider.propose(generation_frame(revision=7))
    finally:
        await client.aclose()
    assert result.value.position_id == "p-a" and result.value.based_on_revision == 7
    assert result.value.text == "折信人暂且停在灯下。\n" + text
    assert len(result.value.operations) == 2
    assert all(operation.value == operation.anchor == text for operation in result.value.operations)
    assert all(operation.anchor in result.value.text for operation in result.value.operations)
    assert result.value.operations[1].role == "尚未说出的约定"
    assert (result.input_tokens, result.output_tokens) == (80, 40)
    schema = json.loads(captured[0]["messages"][0]["content"].split("JSON schema:\n", 1)[1])
    assert schema["required"] == ["fragments"]
    assert "Proposal" not in schema.get("$defs", {})
    assert "based_on_revision" not in schema["properties"]


def test_fragment_schema_restricts_expression_choices_to_their_actual_target_scope():
    frame = generation_frame()
    frame.peer_interfaces = [
        {"position_id": "p-b", "expression_id": "seed-p-b", "role": "主体背景"},
        {"position_id": "p-c", "expression_id": "seed-p-c", "role": "主体背景"},
    ]
    variants = _proposal_schema(frame)["$defs"]["_FragmentTarget"]["oneOf"]
    scopes = {variant["properties"]["target_position_id"]["const"]: variant["properties"] for variant in variants}
    assert set(scopes) == {"p-b", "p-c"}
    assert set(scopes["p-b"]["expression_id"]["enum"]) == {"e-letter", "seed-p-b"}
    assert set(scopes["p-c"]["expression_id"]["enum"]) == {"e-letter", "seed-p-c"}
    assert "enum" not in scopes["p-b"]["role"]  # Narrative roles remain open.


@pytest.mark.parametrize("invalid,reason", [
    ("self", "TARGET_UNKNOWN"), ("unknown_target", "TARGET_UNKNOWN"),
    ("expression", "EXPRESSION_UNAVAILABLE"), ("wrong_peer_seed", "EXPRESSION_UNAVAILABLE"),
    ("empty", "NO_PEER_OPERATION"), ("blank", "EMPTY_FRAGMENT"),
    ("fragment_length", "FRAGMENT_TOO_LONG"), ("total_length", "PROPOSAL_TOO_LARGE"),
    ("operation_count", "TOO_MANY_OPERATIONS"),
])
async def test_fragment_proposals_reject_invalid_scopes_or_limits_without_repair_or_dropped_targets(invalid, reason):
    frame = generation_frame()
    frame.peer_interfaces = [{"position_id": "p-c", "expression_id": "seed-p-c", "role": "主体背景"}]
    payload = fragment_reply()
    fragment = payload["fragments"][0]
    if invalid == "self":
        fragment["targets"][0]["target_position_id"] = "p-a"
    elif invalid == "unknown_target":
        fragment["targets"][0]["target_position_id"] = "not-a-peer"
    elif invalid == "expression":
        fragment["targets"][0]["expression_id"] = "use-letter"
    elif invalid == "wrong_peer_seed":
        fragment["targets"][0]["expression_id"] = "seed-p-c"
    elif invalid == "empty":
        fragment["targets"] = []
    elif invalid == "blank":
        fragment["text"] = " \n\t"
    elif invalid == "fragment_length":
        fragment["text"] = "字" * 1601
    elif invalid == "total_length":
        payload["fragments"].append({"text": "字" * 3500, "targets": []})
    elif invalid == "operation_count":
        fragment["targets"] *= 6
        payload["fragments"].append({"text": "写信人尚未落笔。", "targets": fragment["targets"][:1]})
    provider, client = provider_with_handler(
        lambda request: httpx.Response(200, json=response(json.dumps(payload, ensure_ascii=False)))
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.propose(frame)
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
    assert reason in caught.value.message
    assert caught.value.usage == (80, 40)
    assert "not-a-peer" not in str(caught.value) and "写信人尚未落笔" not in str(caught.value)


async def test_fragment_proposal_accepts_exact_runtime_text_and_operation_boundaries():
    payload = fragment_reply("字" * 1600)
    payload["fragments"][0]["targets"] *= 6
    payload["fragments"].append({"text": "句" * 1899, "targets": []})
    provider, client = provider_with_handler(
        lambda request: httpx.Response(200, json=response(json.dumps(payload, ensure_ascii=False)))
    )
    try:
        value = (await provider.propose(generation_frame())).value
    finally:
        await client.aclose()
    assert len(value.operations) == 12 and len(value.text) == 3500
    assert all(len(operation.anchor) == 1600 for operation in value.operations)


@pytest.mark.parametrize("field", ["text", "target_position_id", "expression_id", "role"])
async def test_fragment_fields_keep_escaped_credentials_out_of_results_and_diagnostics(field):
    payload = fragment_reply()
    if field == "text":
        payload["fragments"][0]["text"] = TEST_CREDENTIAL
    else:
        payload["fragments"][0]["targets"][0][field] = TEST_CREDENTIAL
    escaped = "".join(f"\\u{ord(character):04x}" for character in TEST_CREDENTIAL)
    content = json.dumps(payload, ensure_ascii=False).replace(TEST_CREDENTIAL, escaped)
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.propose(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.code == "MODEL_OUTPUT_INVALID" and caught.value.usage == (80, 40)
    assert TEST_CREDENTIAL not in str(caught.value) + json.dumps(caught.value.public())


@pytest.mark.parametrize("content", ["not json", '{"text":"CoT 参考233"}', '{"text":""}'])
async def test_valid_envelope_settles_rejected_generation_with_exact_safe_usage(content):
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=response(content)))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.usage == (80, 40)
    assert set(caught.value.public()) == {"code", "message", "retryable"}


@pytest.mark.parametrize("envelope", [
    {"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 40}},
    {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": True, "completion_tokens": 40}},
])
async def test_invalid_envelope_keeps_usage_unknown(envelope):
    provider, client = provider_with_handler(lambda request: httpx.Response(200, json=envelope))
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.realize(generation_frame())
    finally:
        await client.aclose()
    assert caught.value.usage is None


def test_shared_interface_recommendation_uses_only_existing_matching_roles_with_stable_tie_break():
    frame = generation_frame()
    frame.active_uses.append({"expression_id": "a-first", "role": "共同生活"})
    frame.peer_interfaces = [
        {"position_id": position_id, "expression_id": expression_id, "role": "共同生活"}
        for expression_id in ("a-first", "e-letter") for position_id in ("p-b", "p-c")
    ]
    selected = _shared_interface(frame)
    assert selected == {"expression_id": "a-first", "role": "共同生活", "target_position_ids": ["p-b", "p-c"]}
    frame.peer_interfaces.reverse()
    assert _shared_interface(frame) == selected
    frame.peer_interfaces = [{"position_id": "p-b", "expression_id": "a-first", "role": "共同生活"}]
    assert _shared_interface(frame) is None


async def test_proposal_prompt_offers_joint_scope_but_excludes_peer_private_content():
    frame = generation_frame()
    frame.peer_interfaces = [
        {"position_id": target, "expression_id": "e-letter", "role": "共同生活", "text": "PRIVATE_PEER_TEXT"}
        for target in ("p-b", "p-c")
    ]

    def handler(request):
        actor = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert actor["recommended_shared_interface"] == {
            "expression_id": "e-letter", "role": "共同生活", "target_position_ids": ["p-b", "p-c"],
        }
        assert "PRIVATE_PEER_TEXT" not in json.dumps(actor)
        return httpx.Response(200, json=response(json.dumps(fragment_reply(), ensure_ascii=False)))

    provider, client = provider_with_handler(handler)
    try:
        assert len((await provider.propose(frame)).value.operations) == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize("usage", [(True, 4), (-1, 4), (1, 5_000_001), (3,), [3, 4], ("3", 4)])
def test_provider_error_accepts_only_a_pair_of_bounded_integer_usage_counts(usage):
    with pytest.raises(ValueError, match="Invalid provider usage accounting"):
        ProviderError("MODEL_OUTPUT_INVALID", "safe", True, usage=usage)
