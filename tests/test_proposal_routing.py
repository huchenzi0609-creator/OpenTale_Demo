"""Provider-owned proposal routing, verified without external network or keys."""

from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import GenerationFrame
from opentale.providers import DeepSeekProvider, OutputFeedback, ProviderError
from opentale.providers.deepseek import _proposal_schema, _routing_table

KEY = "mock-routing-credential"
TEXT = "归来的人停在灯下，等待的人把尚未说完的话留在那里。"


def frame():
    return GenerationFrame(
        position_id="writer", name="留灯人", revision=7,
        seed_text="她没有熄灯。", condition_fingerprint="PRIVATE_DIAGNOSTIC",
        active_uses=[
            {"expression_id": "letter", "role": "尚未寄出的信", "values": ["那封信还在。"]},
            {"expression_id": "seed-writer", "role": "主体背景", "values": ["她的童年。"]},
        ], recent_surface=[],
        peers=[{"id": "reader", "name": "归来的人"}, {"id": "listener", "name": "等待的人"}],
        peer_interfaces=[
            {"position_id": target, "expression_id": "letter", "role": "尚未寄出的信",
             "text": "PRIVATE_PEER_BIOGRAPHY", "values": "PRIVATE_PEER_VALUE",
             "source_position_id": "PRIVATE_CREATOR"}
            for target in ("reader", "listener")
        ] + [
            {"position_id": "reader", "expression_id": "seed-reader", "role": "主体背景"},
            {"position_id": "listener", "expression_id": "seed-listener", "role": "主体背景"},
        ],
    )


def envelope(reply):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(reply, ensure_ascii=False), "reasoning_content": "PRIVATE_REASONING",
    }}], "usage": {"prompt_tokens": 31, "completion_tokens": 17}}


def indexed_reply(current_frame=None):
    routes = _routing_table(current_frame or frame())
    return {"fragments": [{"text": TEXT, "targets": [
        {"interface_index": route["interface_index"], "role": "尚未寄出的信"}
        for route in routes if route["expression_id"] == "letter"
    ]}]}


async def evaluate(reply, current_frame=None):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(reply))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DeepSeekProvider(Settings(api_key=KEY), client).propose(current_frame or frame())
    return result, requests


async def test_new_wire_uses_only_handles_and_compiles_verbatim_into_existing_operation_contract():
    value, requests = await evaluate(indexed_reply())
    assert len(requests) == 1
    proposal = value.value
    assert proposal.position_id == "writer" and proposal.based_on_revision == 7
    assert proposal.text == TEXT
    assert {(op.target_position_id, op.expression_id) for op in proposal.operations} == {
        ("reader", "letter"), ("listener", "letter"),
    }
    assert all(op.value == op.anchor == TEXT and op.role == "尚未寄出的信" for op in proposal.operations)
    assert (value.input_tokens, value.output_tokens) == (31, 17)
    system = requests[0]["messages"][0]["content"]
    schema = json.loads(system.split("JSON schema:\n", 1)[1])
    target = schema["$defs"]["_FragmentTarget"]
    assert target["required"] == ["interface_index", "role"]
    assert set(target["properties"]) == {"interface_index", "role"}
    assert target["properties"]["interface_index"]["enum"] == list(range(6))
    assert "enum" not in target["properties"]["role"]
    example = json.loads(system.split("本阶段完整输出形状示例", 1)[1].split("\n", 1)[1].split("\nJSON schema:", 1)[0])
    assert all(set(target) == {"interface_index", "role"} for target in example["fragments"][0]["targets"])
    actor = json.loads(requests[0]["messages"][1]["content"])
    assert len(actor["recommended_shared_interface"]["interface_indices"]) == 2
    exposed = json.dumps(requests[0]["messages"]) + repr(value)
    for marker in ("PRIVATE_DIAGNOSTIC", "PRIVATE_PEER_BIOGRAPHY", "PRIVATE_PEER_VALUE", "PRIVATE_CREATOR",
                   "PRIVATE_REASONING", KEY):
        assert marker not in exposed


def test_routing_table_is_stable_and_preserves_exact_all_old_writable_pairs():
    current = frame()
    baseline = _routing_table(current)
    current.peers.reverse()
    current.active_uses.reverse()
    current.peer_interfaces.reverse()
    current.peer_interfaces += current.peer_interfaces[:1]  # Duplicate interface is one route, not new authority.
    assert _routing_table(current) == baseline
    assert {(row["target_position_id"], row["expression_id"]) for row in baseline} == {
        ("listener", "letter"), ("listener", "seed-writer"), ("listener", "seed-listener"),
        ("reader", "letter"), ("reader", "seed-writer"), ("reader", "seed-reader"),
    }
    assert all(row["target_position_id"] != current.position_id for row in baseline)
    assert not any(row["target_position_id"] == "reader" and row["expression_id"] == "seed-listener"
                   for row in baseline)
    assert _proposal_schema(current)["$defs"]["_FragmentTarget"]["properties"]["interface_index"]["enum"] == [
        row["interface_index"] for row in baseline
    ]


async def test_exact_peer_background_route_keeps_open_role_and_does_not_change_actor_identity():
    route = next(row for row in _routing_table(frame())
                 if row["target_position_id"] == "reader" and row["expression_id"] == "seed-reader")
    reply = {"fragments": [{"text": TEXT, "targets": [{
        "interface_index": route["interface_index"], "role": "另一种尚未寄出的童年",
    }]}]}
    result, _ = await evaluate(reply)
    operation = result.value.operations[0]
    assert operation.target_position_id == "reader" and operation.expression_id == "seed-reader"
    assert operation.role == "另一种尚未寄出的童年"
    assert result.value.position_id == "writer"


@pytest.mark.parametrize("index", [-1, 6, 999999])
async def test_unknown_handle_is_never_omitted_guessed_or_silently_rerouted(index):
    requests = []
    reply = indexed_reply()
    reply["fragments"][0]["targets"].append({"interface_index": index, "role": "另一种作用"})

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope(reply))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            await DeepSeekProvider(Settings(api_key=KEY), client).propose(frame())
    assert len(requests) == 1  # Retry admission is still the runner's job.
    assert caught.value.feedback == OutputFeedback("PROPOSAL_REFERENCE", ("INTERFACE_UNKNOWN",))
    assert caught.value.usage == (31, 17)
    assert TEXT not in str(caught.value) and KEY not in str(caught.value)


@pytest.mark.parametrize("index", [True, False, "0", 0.0, 1.7, None])
async def test_handle_is_strict_integer_without_coercion(index):
    reply = {"fragments": [{"text": TEXT, "targets": [{"interface_index": index, "role": "尚未寄出的信"}]}]}
    with pytest.raises(ProviderError) as caught:
        await evaluate(reply)
    assert caught.value.feedback == OutputFeedback(
        "SCHEMA_VALIDATION", ("fragments[].targets[].interface_index:int_type",),
    )
    assert caught.value.usage == (31, 17)


@pytest.mark.parametrize("target", ["writer", "unregistered-person", "归来的人", "Reader", " reader "])
async def test_old_named_target_is_accepted_only_as_exact_id_never_a_name_or_fuzzy_alias(target):
    reply = {"fragments": [{"text": TEXT, "targets": [{
        "target_position_id": target, "expression_id": "letter", "role": "尚未寄出的信",
    }]}]}
    with pytest.raises(ProviderError) as caught:
        await evaluate(reply)
    assert caught.value.feedback == OutputFeedback("PROPOSAL_REFERENCE", ("TARGET_UNKNOWN",))
    assert target not in str(caught.value) + json.dumps(caught.value.public())


async def test_explicit_reference_repair_reuses_only_current_valid_table_not_rejected_text_or_id():
    requests = []
    rejected = {"fragments": [{"text": "PRIVATE_REJECTED_PROSE", "targets": [{
        "target_position_id": "PRIVATE_REJECTED_TARGET", "expression_id": "letter", "role": "尚未寄出的信",
    }]}]}

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(rejected if len(requests) == 1 else indexed_reply()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key=KEY), client)
        with pytest.raises(ProviderError) as caught:
            await provider.propose(frame())
        assert len(requests) == 1
        result = await provider.propose(frame(), repair=caught.value.feedback)
    assert len(requests) == 2 and len(result.value.operations) == 2
    assert requests[0]["messages"][1] == requests[1]["messages"][1]
    followup = json.dumps(requests[1]["messages"], ensure_ascii=False)
    assert "PROPOSAL_REFERENCE" in followup and "TARGET_UNKNOWN" in followup
    assert "PRIVATE_REJECTED_PROSE" not in followup and "PRIVATE_REJECTED_TARGET" not in followup
    assert KEY not in followup


@pytest.mark.parametrize("issues", [(), ("ANCHOR_NOT_IN_TEXT",), ("REVISION_MISMATCH",),
                                    ("TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE"), ("target_position_id:missing",), ({},)])
def test_reference_feedback_is_a_finite_single_allowlisted_reason(issues):
    with pytest.raises(ValueError, match="Invalid safe output feedback"):
        OutputFeedback("PROPOSAL_REFERENCE", issues)
