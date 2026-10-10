"""Typed recovery, actual method review and composed methods reach model inputs."""
from __future__ import annotations

import pytest
from opentale.config import Settings
from opentale.domain import Limits
from opentale.growth_kernel import add_link, navigate
from opentale.growth_runner import GrowthRunner, identifier
from opentale.kernel import KernelError
from opentale.providers.base import ModelResult, ProviderError
from opentale.providers.feedback import OutputFeedback
from opentale.providers.growth import FixtureGrowthProvider, validate_decision


@pytest.fixture
def runner(tmp_path):
    value = GrowthRunner(tmp_path, Settings(api_key="offline-method-execution-key"))
    yield value
    value.store.close()


def create(runner, provider="fixture", limits=None):
    return runner.create(title="方法执行回归", provider=provider, windows=[
        {"id": "a", "seed": "她把一杯水放到门前。"}, {"id": "b", "seed": "他在台阶下停住。"},
    ], limits=limits or Limits(max_calls=None, max_tokens=None))["run"]["id"]


def refs(row, kind):
    return {"kind": kind, "object_id": row["id"], "version": row["version"]}


async def test_saved_link_based_method_revision_recovers_without_new_model_call(runner, monkeypatch):
    run_id = create(runner)
    state = runner.load(run_id)
    first, second = [w.entry_node_ids[0] for w in state.windows.values()]
    state = add_link(runner.store, state, first, second, scope_id="window:a", relation="两人的距离", request_id="link")
    link = runner.rows(state, "link", "window:a")[0]
    snapshot = runner.add_reading(run_id, "读轨", [first])
    scope = f"reading:{snapshot['readings'][0]['id']}"
    state = runner.load(run_id)
    frame = runner.frame(state, scope, runner.session(state, scope))
    frame.pop("capabilities")
    frame.pop("work_protocol")
    decision = {"action": "revise", "args": {"method_id": frame["method"]["id"],
        "instruction": "先读台阶下的停顿，再比较递水如何改变两人的距离。", "input_ids": [link["id"]]}}
    validate_decision(frame, decision)
    request_id = f"request-{identifier(scope, 1, 'decide')}"
    runner.put(run_id, "request", {"id": request_id, "private_scope": scope, "scope_id": scope,
        "status": "accepted", "frame": frame, "result": decision, "stage": "decide"})

    class NoNetwork(FixtureGrowthProvider):
        def projection(self, *args, **kwargs):
            raise AssertionError("Recovery must reuse the accepted decision")

    monkeypatch.setattr(runner, "provider", lambda state: NoNetwork())
    snapshot = await runner.step_scope(run_id, scope)
    revised = next(m for m in snapshot["methods"] if m.get("parent_ref"))
    assert refs(link, "link") in revised["source_refs"]
    assert snapshot["run"]["usage"]["calls"] == 0


async def test_generic_view_evidence_has_exact_type_and_private_boundary(runner):
    run_id = create(runner)
    state = runner.load(run_id)
    scope = "window:a"
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    await runner.execute(state, scope, session, {"action": "apply", "args": {"method_id": frame["method"]["id"]}}, frame, "apply", 1)
    runner.put(run_id, "session", session)
    frame = runner.frame(state, scope, session)
    view_ref = refs(session["view"], "view")
    result = await runner.execute(state, scope, session, {"action": "revise", "args": {
        "method_id": frame["method"]["id"], "instruction": "把先前的选材方式作为问题，比较它遗漏的回应。",
        "input_refs": [view_ref],
    }}, frame, "revise", 2)
    revised = runner.store.get(run_id, "method", result["method_id"], include_private=True)
    assert view_ref in revised["source_refs"]
    other_scope = "window:b"
    other_frame = runner.frame(state, other_scope, runner.session(state, other_scope))
    with pytest.raises(KernelError) as error:
        await runner.execute(state, other_scope, runner.session(state, other_scope), {"action": "revise", "args": {
            "method_id": other_frame["method"]["id"], "instruction": "读取私有组织。", "input_refs": [view_ref],
        }}, other_frame, "denied", 2)
    assert error.value.code == "TARGET_UNKNOWN"


class ReviewingFixture(FixtureGrowthProvider):
    def __init__(self):
        self.stages = []

    async def decide(self, frame, *, repair=None):
        self.stages.append("decide")
        if frame["work_seq"] == 0:
            decision = {"action": "try_write", "args": {"parent_ids": [frame["cursor"]]}}
        else:
            decision = {"action": "reflect", "args": {"method_id": frame["method"]["id"],
                "about_refs": [frame["iteration_work"]["last_review_ref"]]}}
        return ModelResult(validate_decision(frame, decision), 10, 3)

    async def draft(self, frame, *, repair=None):
        self.stages.append("draft")
        return ModelResult("她把水递过去，他抬起手，杯子在两人之间停了一会儿。", 20, 8)

    async def review(self, frame, *, repair=None):
        self.stages.append("review")
        return ModelResult("以回答发生前后的距离作为新的比较标准，下一次把停顿与回应共同选入。", 15, 7)


async def test_each_remote_trial_gets_actual_review_and_review_can_itself_be_reviewed(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    provider = ReviewingFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    snapshot = await runner.step_scope(run_id, "window:a")
    first = next(r for r in snapshot["responses"] if r.get("automatic"))
    assert first["review_order"] == 1 and first["review_request_id"]
    assert snapshot["trials"][0]["feedback_refs"] == [refs(first, "response")]
    assert provider.stages == ["decide", "draft", "review"]
    assert snapshot["run"]["usage"]["calls"] == 3
    snapshot = await runner.step_scope(run_id, "window:a")
    second = next(r for r in snapshot["responses"] if r.get("review_order") == 2)
    assert second["about_refs"] == [refs(first, "response")]
    assert snapshot["run"]["usage"]["calls"] == 5
    session = runner.session(runner.load(run_id), "window:a")
    assert session["max_review_order"] == 2
    request = runner.store.get(run_id, "request", second["review_request_id"], include_private=True)
    assert request["frame"]["method_evidence"][0]["object_id"] == first["id"]
    assert request["frame"]["method_evidence"][0]["text"] == runner.body(first)


async def test_review_budget_pause_resumes_only_the_missing_review(runner, monkeypatch):
    run_id = create(runner, "deepseek", Limits(max_calls=2, max_tokens=None, max_windows=None))
    provider = ReviewingFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    with pytest.raises(KernelError) as error:
        await runner.step_scope(run_id, "window:a")
    assert error.value.code == "BUDGET_EXHAUSTED"
    state = runner.load(run_id)
    assert runner.store.count_objects(run_id, "trial") == 1
    state.config["limits"]["max_calls"] = 3
    runner.store.save_head(state)
    snapshot = await runner.step_scope(run_id, "window:a")
    assert provider.stages == ["decide", "draft", "review"]
    assert snapshot["run"]["usage"]["calls"] == 3
    assert runner.store.count_objects(run_id, "trial") == 1


async def test_review_evidence_remains_available_after_newer_feedback_exits_recent_page(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    provider = ReviewingFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    await runner.step_scope(run_id, "window:a")
    state = runner.load(run_id)
    session = runner.session(state, "window:a")
    review_ref = session["last_review_ref"]
    for index in range(25):
        runner.put(run_id, "response", {"id": f"later-{index}", "scope_id": "window:a", "private_scope": "window:a",
            "text_ref": runner.store.put_text("稍后的普通工作回应。").model_dump()})
    frame = runner.frame(state, "window:a", session)
    assert not frame["iteration_work"]["method_review_due"]
    assert frame["iteration_work"]["last_review_ref"] == review_ref
    assert frame["iteration_work"]["formal_review_order"] == 1
    decision = {"action": "reflect", "args": {"method_id": frame["method"]["id"], "about_refs": [review_ref]}}
    validate_decision(frame, decision)


async def test_real_reading_render_gets_review_of_exact_saved_passage(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    snapshot = runner.add_reading(run_id, "读轨")
    track = snapshot["readings"][0]
    scope = f"reading:{track['id']}"
    state = navigate(runner.store, runner.load(run_id), track["id"], track["entry_node_ids"][0],
                     transition="entry", request_id="visit")
    visit = runner.rows(state, "visit", scope)[0]

    class Reading(ReviewingFixture):
        async def decide(self, frame, *, repair=None):
            return ModelResult(validate_decision(frame, {"action": "render", "args": {"visit_ids": [visit["id"]]}}))

        async def render(self, frame, *, repair=None):
            self.stages.append("render")
            return ModelResult("门前放着一杯水，她的手在杯沿停了一下。")

    provider = Reading()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    snapshot = await runner.step_scope(run_id, scope)
    response = snapshot["responses"][0]
    passage = snapshot["readings"][0]["passages"][0]
    assert refs(passage, "passage") in response["about_refs"]
    assert provider.stages == ["render", "review"]
    assert snapshot["run"]["usage"]["calls"] == 3
    request = runner.store.get(run_id, "request", response["review_request_id"], include_private=True)
    assert request["frame"]["method_evidence"][0]["text"] == passage["text"]


async def test_accepted_review_installation_recovers_without_redrafting_or_rebilling(runner, monkeypatch):
    run_id = create(runner, "deepseek")
    provider = ReviewingFixture()
    monkeypatch.setattr(runner, "provider", lambda state: provider)
    original_put = runner.put
    fail_once = True

    def interrupted(run_id, kind, value, **kwargs):
        nonlocal fail_once
        if kind == "response" and value.get("review_request_id") and fail_once:
            fail_once = False
            raise KernelError("TEST_INTERRUPTED", "评议安装中断。")
        return original_put(run_id, kind, value, **kwargs)

    monkeypatch.setattr(runner, "put", interrupted)
    with pytest.raises(KernelError):
        await runner.step_scope(run_id, "window:a")
    usage = dict(runner.load(run_id).config["usage"])
    snapshot = await runner.step_scope(run_id, "window:a")
    assert provider.stages == ["decide", "draft", "review"]
    assert runner.load(run_id).config["usage"] == usage
    assert len(snapshot["responses"]) == len(snapshot["trials"]) == 1
    assert snapshot["trials"][0]["feedback_refs"] == [refs(snapshot["responses"][0], "response")]


async def test_noop_revision_and_repeated_organization_are_visible_no_change_not_format_failures(runner):
    run_id = create(runner)
    scope = "window:a"
    state = runner.load(run_id)
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    before = runner.store.count_objects(run_id, "method")
    decision = {"action": "revise", "args": {"method_id": frame["method"]["id"], "instruction": frame["method"]["instruction"]}}
    validate_decision(frame, decision)
    result = await runner.execute(state, scope, session, decision, frame, "noop", 1)
    assert result["changed"] is False and runner.store.count_objects(run_id, "method") == before
    apply = {"action": "apply", "args": {"method_id": frame["method"]["id"]}}
    first = await runner.execute(state, scope, session, apply, frame, "first", 2)
    second = await runner.execute(state, scope, session, apply, frame, "second", 3)
    assert first["changed"] and not second["changed"]


async def test_safe_validation_feedback_is_preserved_without_rejected_output(runner, monkeypatch):
    run_id = create(runner)

    class Failing(FixtureGrowthProvider):
        async def decide(self, frame, *, repair=None):
            raise ProviderError("MODEL_OUTPUT_INVALID", "句柄无效。", True, usage=(5, 2),
                feedback=OutputFeedback("SCHEMA_VALIDATION", ("HANDLE_UNKNOWN", "input_ids:schema_invalid")))

    monkeypatch.setattr(runner, "provider", lambda state: Failing())
    with pytest.raises(ProviderError):
        await runner.step_scope(run_id, "window:a")
    request = runner.store.get(run_id, "request", f"request-{identifier('window:a', 1, 'decide')}", include_private=True)
    assert request["validation_feedback"]["issues"] == ["HANDLE_UNKNOWN", "input_ids:schema_invalid"]
    assert all(a["validation_feedback"] == request["validation_feedback"] for a in request["attempts"])
    assert "result" not in request and "raw_error" not in request
    session = runner.session(runner.load(run_id), "window:a")
    assert session["error"]["validation_feedback"] == request["validation_feedback"]


async def test_composed_instructions_and_work_material_are_delivered_to_actual_draft(runner, monkeypatch):
    run_id = create(runner)
    scope = "window:a"
    state = runner.load(run_id)
    original = runner.store.get(run_id, "method", state.windows["a"].method_ref.object_id, scope_id=scope)
    other_node = state.windows["b"].entry_node_ids[0]
    method = runner.put(run_id, "method", {**{k: v for k, v in original.items() if k != "version"},
        "id": "composed", "instruction": "把选材方法本身与两人的回应一起比较。",
        "program": {"inputs": [refs(original, "method")], "calls": [
            {"method_ref": refs(original, "method"), "input_refs": [refs(runner.store.get(run_id, 'node', other_node), "node")]},
        ]}})
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    await runner.execute(state, scope, session, {"action": "apply", "args": {"method_id": method["id"]}}, frame, "apply", 1)
    runner.put(run_id, "session", session)
    frame = runner.frame(state, scope, session)
    assert frame["method_execution"]["max_call_depth"] == 1
    assert any(e["kind"] == "method" and e["object_id"] == original["id"] for e in frame["method_evidence"])
    monkeypatch.setattr(runner, "provider", lambda state: FixtureGrowthProvider())
    await runner.execute(state, scope, session, {"action": "try_write", "args": {"parent_ids": [other_node]}}, frame, "draft", 2)
    request = runner.store.get(run_id, "request", f"request-{identifier(scope, 2, 'draft')}", include_private=True)
    assert request["projection"]["frame"]["method_execution"]["max_call_depth"] == 1
    assert other_node in {s["id"] for s in request["projection"]["frame"]["sources"]}
    assert request["projection"]["frame"]["method_evidence"][0]["object"]
