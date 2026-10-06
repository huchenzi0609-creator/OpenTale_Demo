"""Offline request checks; no claim about the model's eventual literary quality."""

from __future__ import annotations

import json

import httpx
import pytest
from opentale.config import Settings
from opentale.domain import GenerationFrame
from opentale.providers import DeepSeekProvider
from opentale.providers.feedback import OutputFeedback
from opentale.providers.prose import NARRATIVE_DISCIPLINE
from opentale.reflection_types import Material, ReflectionFrame


@pytest.mark.parametrize("reflexive", [False, True])
@pytest.mark.parametrize("repairing", [False, True])
async def test_concrete_style_guidance_keeps_real_refusal_and_uses_one_request(reflexive, repairing):
    prior = "她没有答应。对方记住了这次拒绝。"
    frame = GenerationFrame(
        position_id="person-a", name="来客", revision=2, seed_text=prior,
        active_uses=[{
            "expression_id": "shared-answer", "role": "共同经历", "text": prior,
            "values": [prior], "source_position_id": "private-origin-marker",
        }],
        peers=[], peer_interfaces=[], recent_surface=[prior],
        condition_fingerprint="private-fingerprint-marker",
    )
    if reflexive:
        frame.reflection = ReflectionFrame(
            objects=[{"id": "answer-material", "kind": "content", "text": prior}],
            scopes=[{"id": "position:person-a", "name": "来客"}],
            materials=[Material(id="answer-material", text=prior, role="共同经历")],
            instruction="从先前答复继续当前关系。",
            input_fingerprint="private-fingerprint-marker",
        )
    before = frame.model_dump()
    final_text = "她没有答应。她记住了这次约定。"
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"text": final_text}),
                "reasoning_content": "private-reasoning-marker",
            }}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })

    feedback = OutputFeedback("JSON_SYNTAX", ("INVALID_JSON",)) if repairing else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DeepSeekProvider(Settings(api_key="mock-only-credential"), client).realize(
            frame, repair=feedback,
        )

    # Guidance reaches the actual provider boundary, including repair/reflexive inputs.
    assert len(captured) == 1
    payload = captured[0]
    system, user = payload["messages"]
    assert system["content"].startswith(NARRATIVE_DISCIPLINE)
    assert "正文默认采用肯定句" in system["content"]
    assert "严禁用无实义的否定、排除和改名" in system["content"]
    assert "旧段落中的否定句法和辩解节奏不是需要复刻的风格模板" in system["content"]
    assert "方法的 description、instruction 等组织说明" in system["content"]
    assert "在本次输出定稿前做一次简短措辞自检" in system["content"]
    assert ("上次本阶段输出未通过结构校验" in system["content"]) is repairing
    assert prior in user["content"]

    # No word ban, extra editing call, history mutation or reasoning projection.
    assert result.value == final_text
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert frame.model_dump() == before
    assert payload["thinking"] == {"type": "disabled"}
    projection = json.dumps(payload["messages"]) + repr(result)
    assert "private-reasoning-marker" not in projection
    assert "private-origin-marker" not in projection
    assert "private-fingerprint-marker" not in projection
    assert "mock-only-credential" not in projection
