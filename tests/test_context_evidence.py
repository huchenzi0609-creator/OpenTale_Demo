"""A self-consistent request hash must not authenticate fabricated source excerpts."""

import json

from opentale.config import Settings
from opentale.kernel import fingerprint, frame_for
from opentale.presets import initialize
from opentale.providers.deepseek import DeepSeekProvider
from opentale.reflection_types import Material
from opentale.request_evidence import freeze_request, request_chars, verify_request_evidence
from opentale.request_types import RequestAttempt


def test_request_evidence_checks_source_even_when_projection_hashes_are_recomputed(snapshot):
    state = initialize({
        "id": "context", "name": "context",
        "positions": [position.model_dump() for position in snapshot.positions],
        "expressions": [{"id": "shared", "text": "有人答应下一次来访。"}],
    }, "fixture", snapshot.run.limits, "reflexive_v2")
    frame = frame_for(state, state.positions[0].id)
    frame.reflection.materials = [Material(id="shared", text="她答应归还借来的书。\n" * 18000)]
    provider = DeepSeekProvider(Settings(context_target_chars=96000))
    projection = provider.request_projection(frame, "realize")
    request = freeze_request(state, frame, provider, "realize", f"position:{frame.position_id}",
                             projection=projection)
    assert request.context_compaction is not None
    request.attempts.append(RequestAttempt(
        number=1, status="received", request_hash=request.request_hash, projection=projection,
        request_chars=request_chars(projection), input_tokens=0, output_tokens=0,
    ))
    assert verify_request_evidence(state)
    data = json.loads(projection["messages"][1]["content"])
    data["text_pool"][0]["segments"][0] = data["text_pool"][0]["segments"][0].replace("归还", "卖掉", 1)
    projection["messages"][1]["content"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    request.request_hash = fingerprint(projection)
    request.attempts[0].request_hash = request.request_hash
    request.attempts[0].request_chars = request_chars(projection)
    assert not verify_request_evidence(state)
