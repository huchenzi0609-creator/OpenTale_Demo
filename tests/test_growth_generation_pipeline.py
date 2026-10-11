"""Actual method proposals enter recoverable prose work, with advisory review debt."""
from __future__ import annotations

import copy
import json

import pytest
from opentale.config import Settings
from opentale.domain import Limits
from opentale.growth_kernel import add_node, navigate
from opentale.growth_runner import GrowthRunner, identifier
from opentale.growth_types import ObjectRef
from opentale.kernel import KernelError
from opentale.providers.base import ModelResult, ProviderError
from opentale.providers.feedback import OutputFeedback
from opentale.providers.growth import FixtureGrowthProvider, validate_decision


@pytest.fixture
def runner(tmp_path):
    value = GrowthRunner(tmp_path, Settings(api_key="offline-generation-pipeline-key"))
    yield value
    value.store.close()


def create(runner, provider="fixture"):
    return runner.create(title="真实方法调用链", provider=provider, windows=[
        {"id": "a", "seed": "她把杯子放到门前。杯底写着一段尚未访问的字。"},
        {"id": "b", "seed": "他在台阶下等她的答复。"},
    ], limits=Limits(max_calls=None, max_tokens=None))["run"]["id"]


def ref(kind, row):
    return {"kind": kind, "object_id": row["id"], "version": row["version"]}


def current_frame(runner, run_id, scope="window:a"):
    state = runner.load(run_id)
    session = runner.session(state, scope)
    return state, session, runner.frame(state, scope, session)


class PipelineFixture(FixtureGrowthProvider):
    def __init__(self):
        self.stages = []
        self.invoked = []
        self.drafts = []
        self.renders = []
        self.fail_reviews = False
        self.next_action = "try_write"

    async def decide(self, frame, *, repair=None):
        self.stages.append("decide")
        if self.next_action == "try_write":
            args = {"parent_ids": [frame["cursor"]]}
        elif self.next_action == "publish":
            args = {"trial_id": frame["pending_trial"]["id"], "allow_pending_review": True}
        elif self.next_action == "retry_review":
            args = {"debt_id": frame["review_debt"][0]["id"]}
        else:
            raise AssertionError("Test must select a real work action")
        return ModelResult(validate_decision(frame, {"action": self.next_action, "args": args}), 4, 2)

    async def invoke(self, frame, *, repair=None):
        self.stages.append("invoke")
        self.invoked.append(copy.deepcopy(frame))
        children = frame.get("invocation_children", [])
        text = f"操作 {frame['method']['id']}：让约定改变下一次行动。"
        if children:
            text += "落实子方法成果：" + "；".join(child["text"] for child in children)
        return ModelResult(text, 8, 3)

    async def draft(self, frame, *, repair=None):
        self.stages.append("draft")
        self.drafts.append(copy.deepcopy(frame))
        assert frame["method_invocation"]["text"]
        return ModelResult("她把杯子递给他，约定次日一起去看房。", 10, 5)

    async def render(self, frame, *, repair=None):
        self.stages.append("render")
        self.renders.append(copy.deepcopy(frame))
        return ModelResult("\n".join(source["text"] for source in frame["sources"]), 10, 4)

    async def review(self, frame, *, repair=None):
        self.stages.append("review")
        if self.fail_reviews:
            raise ProviderError("MODEL_OUTPUT_INVALID", "公开工作格式待修复。", True, usage=(3, 1),
                                feedback=OutputFeedback("PROSE_INVALID", ("TECHNICAL_PROSE",)))
        return ModelResult("这次约定给另一人留下可选择的去处，下次比较约定改变双方安排的效果。", 6, 3)


async def test_composed_calls_really_execute_postorder_and_root_strategy_enters_draft(runner, monkeypatch):
    run_id = create(runner)
    scope = "window:a"
    state, session, frame = current_frame(runner, run_id)
    child = frame["method"]
    other = runner.store.get(run_id, "node", state.windows["b"].entry_node_ids[0])
    method = runner.put(run_id, "method", {**{key: value for key, value in child.items() if key != "version"},
        "id": "composed-root", "instruction": "用子方法的关系判断组织行动。",
        "program": {"calls": [{"method_ref": ref("method", child), "input_refs": [ref("node", other)]}]}})
    frame = runner.frame(state, scope, session)
    await runner.execute(state, scope, session, {"action": "apply", "args": {"method_id": method["id"]}}, frame, "apply", 1)
    runner.put(run_id, "session", session)
    state, session, frame = current_frame(runner, run_id)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    result = await runner.execute(state, scope, session, {"action": "try_write", "args": {
        "parent_ids": [other["id"]],
    }}, frame, "write", 2)
    assert provider.stages == ["invoke", "invoke", "draft"]
    assert [item["method"]["id"] for item in provider.invoked] == [child["id"], method["id"]]
    child_results = provider.invoked[1]["invocation_children"]
    assert len(child_results) == 1 and child["id"] in child_results[0]["text"]
    strategy = provider.drafts[0]["method_invocation"]
    assert child_results[0]["ref"] in strategy["child_result_refs"]
    assert child_results[0]["text"] in strategy["text"]
    assert strategy["max_call_depth"] == 1 and strategy["status"] == "proposal_not_world_fact"
    trial = runner.store.get(run_id, "trial", result["trial_id"], scope_id=scope)
    assert trial["generation_invocation_ref"] == strategy["ref"]
    assert runner.load(run_id).config["usage"]["calls"] == 3


async def test_equivalent_generation_reuses_saved_invocation_and_feedback_change_reinvokes(runner, monkeypatch):
    run_id = create(runner)
    scope = "window:a"
    state, session, frame = current_frame(runner, run_id)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    first = await runner.prepare_generation(state, scope, session, frame, 1, "draft")
    usage = copy.deepcopy(runner.load(run_id).config["usage"])
    second = await runner.prepare_generation(runner.load(run_id), scope, session, frame, 99, "draft")
    assert second["method_invocation"] == first["method_invocation"]
    assert len(provider.invoked) == 1 and runner.load(run_id).config["usage"] == usage
    feedback = runner.put(run_id, "response", {"id": "new-feedback", "scope_id": scope, "private_scope": scope,
        "text_ref": runner.store.put_text("下一次把约定产生的时间安排选入。").model_dump()})
    changed = {**frame, "responses": [runner.expand(runner.load(run_id), "response", feedback)]}
    third = await runner.prepare_generation(runner.load(run_id), scope, session, changed, 100, "draft")
    assert len(provider.invoked) == 2
    assert third["method_invocation"]["fingerprint"] != first["method_invocation"]["fingerprint"]


async def test_newly_delivered_world_frontier_reinvokes_method_on_new_conditions(runner, monkeypatch):
    run_id = create(runner)
    scope = "window:a"
    state, session, frame = current_frame(runner, run_id)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    first = await runner.prepare_generation(state, scope, session, frame, 1, "draft")
    state = add_node(runner.store, runner.load(run_id), "b", "他把次日的安排告诉了她。", request_id="new-frontier")
    runner.store.save_head(state)
    node = runner.store.get(run_id, "node", state.windows["b"].working_node_id)
    text = runner.body(node)
    changed = {**frame, "world_frontier": [{"id": node["id"], "version": node["version"], "text": text,
        "start": 0, "end": len(text), "full_length": len(text), "partial": False}]}
    second = await runner.prepare_generation(runner.load(run_id), scope, session, changed, 2, "draft")
    assert len(provider.invoked) == 2
    assert second["method_invocation"]["fingerprint"] != first["method_invocation"]["fingerprint"]
    assert provider.invoked[-1]["world_frontier"] == changed["world_frontier"]


async def test_accepted_invocation_installation_recovers_without_recalling_method(runner, monkeypatch):
    run_id = create(runner)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    original_put = runner.put
    fail_once = True
    def interrupt_after_model_acceptance(run_id, kind, value, **kwargs):
        nonlocal fail_once
        if kind == "invocation" and fail_once:
            fail_once = False
            raise KernelError("TEST_INTERRUPTED", "公开方法结果保存中断。")
        return original_put(run_id, kind, value, **kwargs)
    monkeypatch.setattr(runner, "put", interrupt_after_model_acceptance)
    with pytest.raises(KernelError) as captured:
        await runner.step_scope(run_id, "window:a")
    assert captured.value.code == "TEST_INTERRUPTED"
    assert provider.stages == ["decide", "invoke"]
    assert runner.store.count_objects(run_id, "invocation") == 0
    before_calls = runner.load(run_id).config["usage"]["calls"]
    snapshot = await runner.step_scope(run_id, "window:a")
    assert provider.stages == ["decide", "invoke", "draft"]
    assert snapshot["run"]["usage"]["calls"] == before_calls + 1
    assert len(snapshot["trials"]) == len(snapshot["invocations"]) == 1
    assert snapshot["trials"][0]["generation_invocation_ref"] == ref("invocation", snapshot["invocations"][0])


async def test_safe_review_failure_preserves_ready_draft_and_explicit_publication_keeps_debt(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    provider = PipelineFixture()
    provider.fail_reviews = True
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    snapshot = await runner.step_scope(run_id, "window:a")
    assert len(snapshot["trials"]) == 1 and not snapshot["responses"]
    session = runner.session(runner.load(run_id), "window:a")
    assert session["status"] == "ready" and session["phase"] == "review_pending"
    assert session["saved_draft_ref"] and len(session["review_debt"]) == 1
    assert session["review_debt"][0]["status"] == "pending"
    assert snapshot["trials"][0]["feedback_refs"] == []
    assert provider.stages == ["decide", "invoke", "draft", "review", "review", "review"]
    state, session, frame = current_frame(runner, run_id)
    assert frame["review_debt"] and "retry_review" in frame["allowed_actions"]
    with pytest.raises(KernelError) as captured:
        await runner.execute(state, "window:a", session, {"action": "publish", "args": {
            "trial_id": frame["pending_trial"]["id"],
        }}, frame, "unacknowledged-publication", 2)
    assert captured.value.code == "REVIEW_PENDING"
    provider.next_action = "publish"
    before = snapshot["graph_commit"]
    snapshot = await runner.step_scope(run_id, "window:a")
    assert snapshot["graph_commit"] == before + 1
    assert len(snapshot["trials"]) == 1 and len(provider.drafts) == 1 and not snapshot["responses"]
    session = runner.session(runner.load(run_id), "window:a")
    assert session["review_debt"][0]["status"] == "pending"
    assert snapshot["receipts"][-1]["details"]["review_status"] == "pending"


async def test_retry_review_executes_real_review_and_completes_only_its_debt(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    provider = PipelineFixture()
    provider.fail_reviews = True
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    await runner.step_scope(run_id, "window:a")
    debt = runner.session(runner.load(run_id), "window:a")["review_debt"][0]
    provider.fail_reviews = False
    provider.next_action = "retry_review"
    before = copy.deepcopy(runner.load(run_id).config["usage"])
    snapshot = await runner.step_scope(run_id, "window:a")
    assert snapshot["run"]["usage"]["calls"] == before["calls"] + 2
    session = runner.session(runner.load(run_id), "window:a")
    completed = session["review_debt"][0]
    assert completed["id"] == debt["id"] and completed["status"] == "complete"
    assert session["phase"] == "ready_to_publish"
    assert len(snapshot["responses"]) == 1 and len(provider.drafts) == 1
    response = snapshot["responses"][0]
    request = runner.store.get(run_id, "request", response["review_request_id"], include_private=True)
    assert request["status"] == "accepted" and response["about_refs"] == debt["targets"]
    assert snapshot["trials"][0]["feedback_refs"] == [ref("response", response)]
    assert provider.stages[-2:] == ["decide", "review"]


async def test_private_review_output_remains_a_real_error_not_advisory_debt(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    class PrivateReview(PipelineFixture):
        async def review(self, frame, *, repair=None):
            self.stages.append("review")
            raise ProviderError("MODEL_OUTPUT_INVALID", "工作包含不允许公开的内容。", True, usage=(3, 1),
                                feedback=OutputFeedback("PROSE_INVALID", ("WORK_PRIVATE_CONTENT",)))
    provider = PrivateReview()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    with pytest.raises(ProviderError) as captured:
        await runner.step_scope(run_id, "window:a")
    assert captured.value.feedback.issues == ("WORK_PRIVATE_CONTENT",)
    snapshot = runner.snapshot(run_id)
    session = runner.session(runner.load(run_id), "window:a")
    assert len(snapshot["trials"]) == 1 and not snapshot["responses"]
    assert session["status"] == "error" and not session.get("review_debt")
    assert snapshot["run"]["status"] == "error"
    assert len(provider.drafts) == 1


def composed_frame_with_unoffered_child_source(runner, run_id, *, private=False):
    scope = "window:a"
    state = runner.load(run_id)
    opening = state.windows["a"].working_node_id
    text = "邻居答应代收钥匙，并把明日值班的时间写在纸上。"
    state = add_node(runner.store, state, "a" if private else "b", text,
                     private_scope=scope if private else None, request_id="extra-child-source")
    extra_id = state.windows["a" if private else "b"].working_node_id
    extra = runner.store.get(run_id, "node", extra_id, scope_id=scope)
    state.windows["a"].working_node_id = opening
    state.windows["a"].frontier_node_ids = [opening]
    original = runner.store.get(run_id, "method", state.windows["a"].method_ref.object_id, scope_id=scope)
    root = runner.put(run_id, "method", {**{key: value for key, value in original.items() if key != "version"},
        "id": "child-source-root", "instruction": "从子方法实际读到的约定继续构造生活。",
        "program": {"calls": [{"method_ref": ref("method", original), "input_refs": [ref("node", extra)]}]}})
    state.windows["a"].method_ref = ObjectRef.model_validate(ref("method", root))
    runner.store.save_head(state)
    state, session, frame = current_frame(runner, run_id)
    # A saved method can refer to a scoped input outside the current catalog;
    # the invocation must actually deliver its text and freeze what was read.
    frame["nodes"] = [row for row in frame["nodes"] if row["id"] != extra_id]
    frame["world_frontier"] = [row for row in frame["world_frontier"] if row["id"] != extra_id]
    frame["capabilities"] = [row for row in frame["capabilities"] if row["object_id"] != extra_id]
    assert extra_id not in {row["object_id"] for row in frame["capabilities"]}
    assert extra_id not in {row["id"] for row in frame["sources"]}
    return state, session, frame, extra


async def test_child_reads_unoffered_scoped_input_and_freezes_exact_generation_source_span(runner, monkeypatch):
    run_id = create(runner)
    state, session, frame, extra = composed_frame_with_unoffered_child_source(runner, run_id)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    result = await runner.execute(state, "window:a", session, {"action": "try_write", "args": {
        "parent_ids": [frame["cursor"]],
    }}, frame, "unoffered-child-write", 1)
    extra_text = runner.body(extra)
    child_source = next(row for row in provider.invoked[0]["sources"] if row["id"] == extra["id"])
    assert child_source["text"] == extra_text
    assert (child_source["start"], child_source["end"]) == (0, len(extra_text))
    draft = provider.drafts[0]
    assert any(source["id"] == extra["id"] and source["text"] == extra_text for source in draft["sources"])
    trial = runner.store.get(run_id, "trial", result["trial_id"], scope_id="window:a")
    candidate = runner.store.get(run_id, "candidate", trial["result_ref"]["object_id"], scope_id="window:a")
    span = next(span for span in candidate["source_spans"] if span["node_id"] == extra["id"])
    assert span == {"node_id": extra["id"], "node_version": extra["version"], "text_ref": extra["text_ref"],
                    "start": 0, "end": len(extra_text), "passage_start": None, "passage_end": None}
    assert ref("node", extra) in trial["source_refs"]
    assert trial["generation_invocation_ref"] in trial["source_refs"]


async def test_private_child_read_keeps_publication_private_and_peer_cannot_read(runner, monkeypatch):
    run_id = create(runner)
    state, session, frame, extra = composed_frame_with_unoffered_child_source(runner, run_id, private=True)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    result = await runner.execute(state, "window:a", session, {"action": "try_write", "args": {
        "parent_ids": [frame["cursor"]],
    }}, frame, "private-child-write", 1)
    assert any(source["id"] == extra["id"] for source in provider.invoked[0]["sources"])
    runner.put(run_id, "session", session)
    state, session, frame = current_frame(runner, run_id)
    published = await runner.execute(state, "window:a", session, {"action": "publish", "args": {
        "trial_id": result["trial_id"],
    }}, frame, "private-child-publish", 2)
    node = runner.store.get(run_id, "node", published["node_id"], scope_id="window:a")
    assert node["private_scope"] == "window:a"
    assert any(span["node_id"] == extra["id"] for span in node["source_spans"])
    assert runner.store.get(run_id, "node", node["id"], scope_id="window:b") is None
    assert runner.store.get(run_id, "node", extra["id"], scope_id="window:b") is None
    _, _, peer_frame = current_frame(runner, run_id, "window:b")
    assert node["id"] not in {cap["object_id"] for cap in peer_frame["capabilities"]}


async def test_frozen_v2_draft_reconstruction_preserves_original_trial_source_semantics(runner, monkeypatch):
    run_id = create(runner)
    state, session, frame = current_frame(runner, run_id)
    frozen = copy.deepcopy(frame)
    frozen["work_protocol"] = "growth-work-v2"
    frozen.pop("method_invocation", None)
    frozen["parent_ids"] = [frame["cursor"]]
    draft_request = f"request-{identifier('window:a', 1, 'draft')}"
    runner.put(run_id, "request", {"id": draft_request, "scope_id": "window:a", "private_scope": "window:a",
        "stage": "draft", "status": "accepted", "frame": frozen, "result": "她将杯子递给等在台阶下的人。"})
    class SavedOnly(FixtureGrowthProvider):
        def projection(self, *args, **kwargs):
            raise AssertionError("Accepted legacy prose must recover without a model call")
    monkeypatch.setattr(runner, "provider", lambda state: SavedOnly())
    result = await runner.execute(state, "window:a", session, {"action": "try_write", "args": {
        "parent_ids": [frame["cursor"]],
    }}, frame, "legacy-reconstruction", 1)
    trial = runner.store.get(run_id, "trial", result["trial_id"], scope_id="window:a")
    assert trial["source_refs"] == [{"kind": "node", "object_id": row["id"], "version": row["version"]}
                                    for row in frozen["sources"]]
    assert "generation_invocation_ref" not in trial
    assert runner.load(run_id).config["usage"]["calls"] == 0
    assert runner.store.count_objects(run_id, "invocation") == 0


async def test_reading_invocation_uses_only_visited_source_and_snapshot_retains_generation_ref(runner, monkeypatch):
    run_id = create(runner)
    snapshot = runner.add_reading(run_id, "局部读轨")
    track = snapshot["readings"][0]
    scope = f"reading:{track['id']}"
    state = navigate(runner.store, runner.load(run_id), track["id"], track["entry_node_ids"][0],
        transition="entry", request_id="partial-visit", start=0, end=8)
    visit = runner.rows(state, "visit", scope)[0]
    state, session, frame = current_frame(runner, run_id, scope)
    provider = PipelineFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    await runner.execute(state, scope, session, {"action": "render", "args": {"visit_ids": [visit["id"]]}}, frame, "render", 1)
    assert provider.stages == ["invoke", "render"]
    invoked, rendered = provider.invoked[0], provider.renders[0]
    assert invoked["invocation_context"]["mode"] == "render"
    assert invoked["sources"] == rendered["sources"]
    assert [(source["id"], source["start"], source["end"]) for source in rendered["sources"]] == [
        (visit["node_id"], 0, 8),
    ]
    assert "尚未访问的字" not in json.dumps(invoked, ensure_ascii=False)
    assert "他在台阶下等她的答复" not in json.dumps(invoked, ensure_ascii=False)
    passage = runner.snapshot(run_id)["readings"][0]["passages"][0]
    assert passage["generation_invocation_ref"] == rendered["method_invocation"]["ref"]
    assert passage["text"] == rendered["sources"][0]["text"]
    assert passage["source_spans"] == visit["source_spans"]


async def test_healthy_reader_never_masks_another_window_error(runner, monkeypatch):
    run_id = create(runner)
    snapshot = runner.add_reading(run_id, "健康读轨")
    scope = f"reading:{snapshot['readings'][0]['id']}"
    class OneBrokenWindow(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            if frame["scope_id"] == "window:a":
                raise ProviderError("PROVIDER_UNAVAILABLE", "测试连接失败。", True)
            return await super().decide(frame, repair=repair)
    monkeypatch.setattr(runner, "provider", lambda state: OneBrokenWindow())
    with pytest.raises(ProviderError):
        await runner.step_scope(run_id, "window:a")
    snapshot = await runner.step_scope(run_id, scope)
    assert snapshot["readings"][0]["status"] == "ready"
    assert snapshot["windows"][0]["status"] == "error"
    assert snapshot["run"]["status"] == "error"
    assert runner.load(run_id).status == "error"
