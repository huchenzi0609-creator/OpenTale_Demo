"""Recover bounded reads and ground method work in actual local results, offline."""
from __future__ import annotations

import pytest
from opentale.config import Settings
from opentale.growth_kernel import add_node, navigate
from opentale.growth_runner import GrowthRunner, identifier
from opentale.kernel import KernelError
from opentale.providers.growth import FixtureGrowthProvider


@pytest.fixture
def runner(tmp_path):
    instance = GrowthRunner(tmp_path, Settings(api_key="offline-learning-dummy-key"))
    yield instance
    instance.store.close()


def create(runner):
    return runner.create(title="学习回归", provider="fixture", windows=[
        {"id": "door", "label": "门前", "seed": "甲😀乙。", "direction": "看门前的回应"},
        {"id": "stairs", "label": "台阶", "seed": "台阶有雨。", "direction": ""},
    ])["run"]["id"]


async def execute(runner, run_id, scope, decision, sequence=1):
    state = runner.load(run_id)
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    result = await runner.execute(state, scope, session, decision, frame, f"offline-{sequence}", sequence)
    runner.put(run_id, "session", session)
    return result


@pytest.mark.parametrize("start,end,actual", [(0, 2400, (0, 4)), (1, 3, (1, 3)),
                                                (4, 2400, (4, 4)), (2400, 3000, (4, 4))])
async def test_page_capacity_clips_at_unicode_eof_without_inventing_ranges(runner, start, end, actual):
    run_id = create(runner)
    node_id = runner.load(run_id).windows["door"].entry_node_ids[0]
    result = await execute(runner, run_id, "window:door", {"action": "read_page", "args": {
        "kind": "node", "id": node_id, "start": start, "end": end,
    }})
    page = result["read"]
    assert (page["start"], page["end"]) == actual
    assert page["text"] == "甲😀乙。"[actual[0]:actual[1]]
    assert (page["requested_start"], page["requested_end"]) == (start, end)
    assert page["clipped"] == (actual != (start, end))
    assert page["eof"] == (actual[1] == 4)
    assert page["full_length"] == 4
    assert page["next_start"] == (actual[1] if actual[1] < 4 else None)


@pytest.mark.parametrize("start,end", [(-1, 1), (3, 2), (True, 3)])
async def test_page_capacity_still_rejects_invalid_ranges(runner, start, end):
    run_id = create(runner)
    node_id = runner.load(run_id).windows["door"].entry_node_ids[0]
    with pytest.raises(KernelError) as captured:
        await execute(runner, run_id, "window:door", {"action": "read_page", "args": {
            "kind": "node", "id": node_id, "start": start, "end": end,
        }})
    assert captured.value.code == "SOURCE_RANGE_INVALID"


@pytest.mark.parametrize("role", ["window", "reading"])
async def test_saved_accepted_trial_read_recovers_without_any_new_model_call(runner, monkeypatch, role):
    run_id = create(runner)
    if role == "reading":
        snapshot = runner.add_reading(run_id, "读轨")
        scope = f"reading:{snapshot['readings'][0]['id']}"
    else:
        scope = "window:door"
    state = runner.load(run_id)
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    candidate = runner.put(run_id, "candidate", {"id": "cached-candidate", "private_scope": scope,
        "text_ref": runner.store.put_text("甲😀乙。").model_dump(), "parent_ids": []})
    trial = runner.put(run_id, "trial", {"id": "cached-trial", "scope_id": scope, "private_scope": scope,
        "method_ref": runner.actor(state, scope).method_ref.model_dump(), "source_refs": [],
        "result_ref": {"kind": "candidate", "object_id": candidate["id"], "version": candidate["version"]},
        "feedback_refs": [], "adopted": False})
    session.update(pending_trial_id=trial["id"], status="error", error={"code": "SOURCE_RANGE_INVALID"})
    runner.put(run_id, "session", session)
    request_id = f"request-{identifier(scope, 1, 'decide')}"
    decision = {"action": "read_page", "args": {"kind": "trial", "id": trial["id"], "start": 0, "end": 2400}}
    accepted = runner.put(run_id, "request", {"id": request_id, "scope_id": scope, "private_scope": scope,
        "stage": "decide", "status": "accepted", "frame": frame, "result": decision})
    before_usage = runner.snapshot(run_id)["run"]["usage"]

    class NoNetwork(FixtureGrowthProvider):
        def projection(self, *args, **kwargs):
            raise AssertionError("An accepted decision must not make another request")

    monkeypatch.setattr(runner, "provider", lambda state: NoNetwork())
    snapshot = await runner.step_scope(run_id, scope)
    recovered = runner.session(runner.load(run_id), scope)
    assert recovered["work_seq"] == 1 and recovered["status"] == "ready"
    assert recovered["pages"][0]["text"] == "甲😀乙。"
    assert recovered["pages"][0]["end"] == 4
    assert snapshot["run"]["usage"] == before_usage
    assert runner.store.get(run_id, "request", request_id, include_private=True) == accepted
    next_frame = runner.frame(runner.load(run_id), scope, recovered)
    assert next_frame["pending_trial"]["full_length"] == 4
    assert not next_frame["pending_trial"]["partial"]


async def test_bootstrap_follows_new_frontier_and_sparse_revision_preserves_dynamic_selection(runner):
    run_id = create(runner)
    state = runner.load(run_id)
    old_ref = state.windows["door"].method_ref
    state = add_node(runner.store, state, "door", "她把温水递了过去。", request_id="new-frontier")
    current = state.windows["door"].working_node_id
    applied = await execute(runner, run_id, "window:door", {"action": "apply", "args": {
        "method_id": old_ref.object_id,
    }})
    assert current in applied["selected_node_ids"]
    revised = await execute(runner, run_id, "window:door", {"action": "revise", "args": {
        "method_id": old_ref.object_id, "instruction": "比较递水前后的迟疑，以回答检验两人的距离。",
        "program": {"groups": {"这次回应": [current]}, "purpose": "观察回答造成的变化"},
    }}, 2)
    method = runner.store.get(run_id, "method", revised["method_id"], version=revised["version"], include_private=True)
    assert "node_ids" not in method["program"]
    again = await execute(runner, run_id, "window:door", {"action": "revise", "args": {
        "method_id": revised["method_id"], "instruction": "把回答与此前的等待共同读入，再观察一次行动。",
    }}, 3)
    retained = runner.store.get(run_id, "method", again["method_id"], include_private=True)
    assert retained["program"] == method["program"]


async def test_local_trial_direction_does_not_accumulate_as_long_term_policy(runner):
    run_id = create(runner)
    scope = "window:door"
    old = runner.load(run_id).windows["door"].direction
    await execute(runner, run_id, scope, {"action": "try_write", "args": {
        "direction": "用递水时的停顿观察是否得到回应",
    }})
    state = runner.load(run_id)
    assert state.windows["door"].direction == old
    session = runner.session(state, scope)
    assert session["last_trial_direction"] == "用递水时的停顿观察是否得到回应"
    frame = runner.frame(state, scope, session)
    assert frame["direction"] == old
    assert frame["method_work"]["bootstrap"]
    assert frame["method_work"]["pending_trial_unpublished"]
    assert frame["method_work"]["recent_result_refs"][0]["kind"] == "trial"


async def test_adoption_does_not_claim_organization_was_already_executed(runner):
    run_id = create(runner)
    scope = "window:door"
    original = runner.load(run_id).windows["door"].method_ref.model_dump()
    await execute(runner, run_id, scope, {"action": "apply", "args": {"method_id": original["object_id"]}})
    revised = await execute(runner, run_id, scope, {"action": "revise", "args": {
        "method_id": original["object_id"], "instruction": "先读对方的回应，再读递水的动作，比较距离。",
    }}, 2)
    await execute(runner, run_id, scope, {"action": "adopt", "args": {"method_id": revised["method_id"]}}, 3)
    state = runner.load(run_id)
    frame = runner.frame(state, scope, runner.session(state, scope))
    assert frame["method_work"]["adopted_ref"]["object_id"] == revised["method_id"]
    assert frame["method"]["id"] == revised["method_id"]
    assert frame["method_work"]["applied_ref"] == original
    await execute(runner, run_id, scope, {"action": "apply", "args": {"method_id": revised["method_id"]}}, 4)
    state = runner.load(run_id)
    frame = runner.frame(state, scope, runner.session(state, scope))
    assert frame["method_work"]["applied_ref"] == frame["method_work"]["adopted_ref"]


async def test_legacy_generated_direction_is_contextual_history_not_repeated_policy(runner):
    run_id = create(runner)
    scope = "window:door"
    state = runner.load(run_id)
    first_frame = runner.frame(state, scope, runner.session(state, scope))
    runner.put(run_id, "request", {"id": f"request-{identifier(scope, 1, 'decide')}",
        "scope_id": scope, "private_scope": scope, "status": "accepted", "frame": first_frame})
    state.windows["door"].direction = "绝不重复，绝不出声，删掉所有解释。"
    runner.store.save_head(state)
    session = runner.session(state, scope)
    frame = runner.frame(state, scope, session)
    assert frame["direction"] == first_frame["direction"] == "看门前的回应"
    assert runner.load(run_id).windows["door"].direction == "绝不重复，绝不出声，删掉所有解释。"


async def test_reading_method_can_use_own_exact_passage_and_pending_visits_are_explicit(runner):
    from opentale.growth_kernel import add_passage

    run_id = create(runner)
    snapshot = runner.add_reading(run_id, "读轨")
    track = snapshot["readings"][0]
    scope = f"reading:{track['id']}"
    state = navigate(runner.store, runner.load(run_id), track["id"], track["entry_node_ids"][0],
                     transition="entry", request_id="visit", start=0, end=4)
    visits = runner.rows(state, "visit", scope)
    frame = runner.frame(state, scope, runner.session(state, scope))
    assert frame["reading_work"]["unrendered_visit_ids"] == [visits[0]["id"]]
    state = add_passage(runner.store, state, track["id"], "两人在门前交换了一次目光。",
                        visit_ids=[visits[0]["id"]], request_id="render")
    passage = runner.rows(state, "passage", scope)[0]
    frame = runner.frame(state, scope, runner.session(state, scope))
    assert frame["reading_work"]["unrendered_visit_ids"] == []
    revised = await execute(runner, run_id, scope, {"action": "revise", "args": {
        "method_id": track["method_ref"]["object_id"], "instruction": "以人物回应的先后组织路径，比较距离变化。",
        "input_ids": [passage["id"]],
    }})
    method = runner.store.get(run_id, "method", revised["method_id"], include_private=True)
    assert {"kind": "passage", "object_id": passage["id"], "version": passage["version"]} in method["source_refs"]
