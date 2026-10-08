"""Frozen v2 context projection remains deterministic and API-free."""
from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from opentale.config import Settings
from opentale.policy_types import View, ViewGroup
from opentale.providers import DeepSeekProvider as CurrentDeepSeekProvider
from opentale.providers import ProviderError
from opentale.providers.context import (
    LEGACY_PROJECTION_VERSION,
    PROJECTION_VERSION,
    verify_compaction_sources,
)
from opentale.providers.feedback import OutputFeedback
from opentale.reflection_types import Material
from test_reflexive_provider import envelope
from test_reflexive_v2_provider import v2_frame


def DeepSeekProvider(*args, **kwargs):
    kwargs.setdefault("projection_version", PROJECTION_VERSION)
    return CurrentDeepSeekProvider(*args, **kwargs)


def repeated_frame():
    frame = v2_frame()
    text = "她走到桥边，记得先前已经答应陪那个人回去。" * 1000
    frame.reflection.materials = [Material(id=f"material-{i}", text=text, role=f"经历{i}") for i in range(16)]
    frame.reflection.view = View(material_ids=[m.id for m in frame.reflection.materials], groups=[
        ViewGroup(id=f"group-{i}", material_ids=[m.id]) for i, m in enumerate(frame.reflection.materials)])
    frame.reflection.objects.extend({"id": f"candidate-{i}", "kind": "view", "text": "历史入口",
                                     "version": f"version-{i}", "materials": [],
                                     "view": {"groups": [{"id": f"candidate-{i}-{j}", "label": "过去的组织",
                                                           "material_ids": [], "children": []} for j in range(20)],
                                              "relations": [], "material_ids": []}}
                                    for i in range(100))
    return frame


def user_data(projection):
    return json.loads(projection["messages"][1]["content"])


def reconstruct(data):
    owner = data.get("reflection", data)
    pool = data["text_pool"]
    return [{**{key: value for key, value in row.items() if key != "text_ref"},
             "text": "".join(pool[row["text_ref"]]["segments"])} for row in owner["materials"]]


def test_lossless_pool_keeps_all_occurrences_view_indices_and_request_capabilities():
    frame = repeated_frame()
    before = frame.model_dump()
    provider = DeepSeekProvider(Settings(context_target_chars=250_000))
    projection = provider.request_projection(frame, "propose")
    data = user_data(projection)
    manifest = data["context_compaction"]
    assert manifest["lossy"] is False
    assert len(data["text_pool"]) == 1
    assert len(data["reflection"]["materials"]) == 16
    assert [row["role"] for row in reconstruct(data)] == [f"经历{i}" for i in range(16)]
    assert [row["text"] for row in reconstruct(data)] == [m.text for m in frame.reflection.materials]
    assert data["reflection"]["view"]["material_indices"] == list(range(16))
    assert [row["object_index"] for row in data["reflection"]["objects"]] == list(range(len(frame.reflection.objects)))
    assert [row["version"] for row in data["reflection"]["objects"]] == [row["version"] for row in frame.reflection.objects]
    assert frame.model_dump() == before
    assert verify_compaction_sources(frame, projection)
    assert manifest["output_chars"] == sum(len(message["content"]) for message in projection["messages"])
    assert manifest["input_chars"] > manifest["output_chars"]


def test_excerpt_spans_keep_source_sentences_and_report_all_omissions():
    frame = v2_frame(reading=True)
    paragraphs = [f"第{i}次归来时，她答应陪对方回去。她没有忘记这项承诺。\n" for i in range(2000)]
    text = "".join(paragraphs)
    frame.reflection.materials = [Material(id="long-life", text=text, role="主体背景")]
    frame.reflection.view = View(material_ids=["long-life"], groups=[ViewGroup(id="life", material_ids=["long-life"])])
    frame.previous_segments = [{"id": f"segment-{i}", "revision": i, "text": f"这次她记起了第{i}封信。",
                               "focus_position_id": "person-a", "expression_ids": [], "reinterprets": []}
                              for i in range(5)]
    projection = DeepSeekProvider(Settings(context_target_chars=20_000)).request_projection(frame, "present")
    data = user_data(projection)
    assert data["context_compaction"]["lossy"] is True
    assert data["context_compaction"]["omitted_text_chars"] > 0
    assert len(data["previous_segments"]) == len(frame.previous_segments)
    assert data["history_table"][-1]["history_index"] == 4
    for row in data["previous_segments"][-2:]:
        assert data["text_pool"][row["text_ref"]]["omitted_chars"] == 0
    entry = data["text_pool"][0]
    assert entry["spans"][0][0] == 0 and entry["spans"][-1][1] == len(text)
    for (start, end), segment in zip(entry["spans"], entry["segments"], strict=True):
        assert segment == text[start:end]
        assert end == len(text) or text[end - 1] in "。\n"
    assert verify_compaction_sources(frame.model_dump(), projection)
    changed = deepcopy(projection)
    bad_data = user_data(changed)
    bad_data["text_pool"][0]["segments"][0] = "虚构的摘要"
    changed["messages"][1]["content"] = json.dumps(bad_data)
    assert verify_compaction_sources(frame, changed) is False


def test_repair_preserves_identical_compacted_user_input_and_base_manifest():
    frame = repeated_frame()
    provider = DeepSeekProvider(Settings(context_target_chars=22_000))
    before = provider.request_projection(frame, "propose")
    repaired = provider.request_projection(frame, "propose", repair=OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)))
    assert before["messages"][1] == repaired["messages"][1]
    assert before["messages"][0] != repaired["messages"][0]
    assert user_data(repaired)["context_compaction"]["output_chars"] == sum(len(m["content"]) for m in before["messages"])


def test_old_projection_adapter_preserves_old_format_and_is_independent():
    frame = v2_frame()
    provider = DeepSeekProvider(Settings(context_target_chars=1))
    old = provider.with_projection_version(LEGACY_PROJECTION_VERSION)
    assert old is not provider and old._client is provider._client
    assert old.projection_version == LEGACY_PROJECTION_VERSION
    assert provider.projection_version == PROJECTION_VERSION
    data = user_data(old.request_projection(frame, "propose"))
    assert "text_pool" not in data and "context_compaction" not in data
    assert "text" in data["reflection"]["materials"][0]
    # Legacy capacity errors remain local and do not become paid model calls.
    with pytest.raises(ProviderError, match="容量"):
        old.request_projection(repeated_frame(), "propose")


@pytest.mark.parametrize("stage", ["propose", "realize", "present"])
async def test_compacted_projection_equals_actual_sent_body_with_no_second_model_call(stage):
    frame = repeated_frame() if stage != "present" else v2_frame(reading=True)
    if stage == "present":
        frame.reflection.materials[0].text *= 5000
    wire = {"text": "她收好雨衣，走进厨房。"}
    if stage == "propose":
        wire = {"fragments": [{"text": wire["text"], "targets": []}]}
    elif stage == "present":
        wire.update(focus_index=0, expression_indices=[], reinterprets_indices=[])
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(wire))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(Settings(api_key="mock-only-secret", context_target_chars=20_000), client)
        expected = provider.request_projection(frame, stage)
        await getattr(provider, stage)(frame)
    assert captured == [expected]
    assert verify_compaction_sources(frame, expected)
    assert "mock-only-secret" not in json.dumps(expected)


def test_context_target_is_independent_and_has_no_configuration_ceiling():
    assert Settings().context_target_chars == 96_000
    assert Settings(context_target_chars=10**12).context_target_chars == 10**12


def test_repeated_metadata_is_losslessly_pooled_without_merging_material_occurrences():
    frame = v2_frame()
    frame.reflection.materials = [Material(id="shared-material", text="他已经答应回去。" * 1000,
                                         role="这段经验在不同位置的共同作用" * 8) for _ in range(30)]
    frame.reflection.view = View(material_ids=["shared-material"] * 30, groups=[
        ViewGroup(id=f"group-{index}", material_ids=["shared-material"]) for index in range(30)])
    projection = DeepSeekProvider(Settings(context_target_chars=80_000)).request_projection(frame, "realize")
    data = user_data(projection)
    assert len(data["material_metadata"]) == 1 and len(data["materials"]) == 30
    assert all(row["metadata_ref"] == 0 for row in data["materials"])
    assert data["view"]["material_indices"] == list(range(30))
    assert verify_compaction_sources(frame, projection)


def test_enormous_unpunctuated_text_uses_declared_exact_partial_sentence_ranges():
    frame = v2_frame()
    frame.reflection.materials = [Material(id="long-utterance", text="长句" * 350_000)]
    frame.reflection.view = View(material_ids=["long-utterance"],
                                 groups=[ViewGroup(id="utterance", material_ids=["long-utterance"])])
    projection = DeepSeekProvider(Settings(context_target_chars=24_000)).request_projection(frame, "realize")
    data = user_data(projection)
    assert data["text_pool"][0]["partial_sentence"] is True
    assert data["text_pool"][0]["omitted_chars"] > 0
    assert verify_compaction_sources(frame, projection)
    assert sum(len(message["content"]) for message in projection["messages"]) < 512_000
    assert "materials 和 instruction 保持完整" not in projection["messages"][0]["content"]


def test_classic_and_v1_request_bodies_do_not_change_with_projection_adapter():
    frame = v2_frame()
    frame.reflection.language_version = 1
    provider = DeepSeekProvider(Settings(context_target_chars=1))
    old = provider.with_projection_version(LEGACY_PROJECTION_VERSION)
    for candidate in (frame, frame.model_copy(update={"reflection": None})):
        assert provider.request_projection(candidate, "realize") == old.request_projection(candidate, "realize")
