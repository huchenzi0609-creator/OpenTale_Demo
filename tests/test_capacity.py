"""Expanded capacity and checkpoint budget edits preserve work and accounting."""

import pytest
from fastapi.testclient import TestClient
from opentale.api import create_app
from opentale.capacity import DEFAULT_TOKEN_BUDGET, MAX_TOKEN_BUDGET, budget_bounds
from opentale.domain import Limits, Pending
from opentale.kernel import frame_for
from opentale.providers import FixtureProvider
from opentale.replay import verify_replay
from pydantic import ValidationError


def test_budget_defaults_remain_finite_but_unlimited_and_large_values_are_accepted():
    assert Limits().max_tokens == DEFAULT_TOKEN_BUDGET == 128000
    assert Limits(**budget_bounds()).model_dump() == dict.fromkeys(budget_bounds())
    assert Limits(max_tokens=MAX_TOKEN_BUDGET + 1).max_tokens == 5120001
    larger = Limits(max_calls=100000, max_tokens=512000000, max_windows=100000, max_parallel=1000)
    assert larger.max_parallel == 1000 and larger.max_tokens == 512000000
    for field in budget_bounds():
        with pytest.raises(ValidationError):
            Limits(**{field: 0})


def test_api_publishes_unbounded_budgets_and_accepts_them_without_generating(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        assert client.get("/api/config").json()["budget_bounds"] == budget_bounds()
        assert client.post("/api/config/reload").json()["budget_bounds"] == budget_bounds()
        result = client.post("/api/runs", json={
            "preset_id": "letters", "provider": "fixture", "limits": budget_bounds(),
        })
        assert result.status_code == 200 and result.json()["run"]["usage"]["calls"] == 0
        larger = client.post("/api/runs", json={
            "preset_id": "letters", "provider": "fixture", "limits": {"max_tokens": 5120001},
        })
        assert larger.status_code == 200 and larger.json()["run"]["usage"]["calls"] == 0
        assert result.json()["run"]["limits"] == budget_bounds()
        assert result.json()["schema_version"] == 4
        assert larger.json()["schema_version"] == 4


async def test_update_budget_preserves_saved_proposals_usage_and_old_error(tmp_path):
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        state = app.state.store.load(client.post("/api/runs", json={"preset_id": "letters"}).json()["run"]["id"])
        position = state.positions[0].id
        proposal = (await FixtureProvider().propose(frame_for(state, position))).value
        state.pending = Pending(base_revision=0, proposals={position: proposal})
        state.run.status = "error"
        state.run.phase = "drafting"
        state.run.last_error = {"code": "MODEL_OUTPUT_INVALID", "message": "测试拒绝。", "retryable": True}
        state.run.usage.calls = 6
        state.run.usage.input_tokens = 1000
        state.run.usage.output_tokens = 200
        state.run.usage.unknown_tokens = 300
        state.run.usage.failed_calls = 1
        app.state.store.save(state)
        path = f"/api/runs/{state.run.id}"
        before = client.get(path).json()
        limits = {**before["run"]["limits"], "max_tokens": 512000, "max_calls": 48}
        updated = client.patch(path + "/limits", json=limits)
        assert updated.status_code == 200
        after = updated.json()
        assert after["run"]["id"] == state.run.id
        assert after["run"]["usage"] == before["run"]["usage"]
        assert after["run"]["last_error"] == before["run"]["last_error"]
        assert after["pending"] == before["pending"]
        for key in ("positions", "expressions", "uses", "continuations", "story", "reading"):
            assert after[key] == before[key]
        assert after["events"][-1]["type"] == "limits_updated"
        assert app.state.store.load(state.run.id).run.limits.max_tokens == 512000
        for lowered in ({"max_calls": 5}, {"max_tokens": 1499}):
            invalid = client.patch(path + "/limits", json={**limits, **lowered})
            assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "BUDGET_BELOW_USAGE"
            assert client.get(path).json() == after


def test_update_windows_below_used_and_while_running_is_rejected(tmp_path):
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        created = client.post("/api/runs", json={"preset_id": "letters"}).json()
        path = f"/api/runs/{created['run']['id']}"
        state = app.state.store.load(created["run"]["id"])
        state.run.usage.windows = 2
        app.state.store.save(state)
        limits = {**created["run"]["limits"], "max_windows": 1}
        rejected = client.patch(path + "/limits", json=limits)
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "BUDGET_BELOW_USAGE"
        state.run.status = "running"
        app.state.store.save(state)
        busy = client.patch(path + "/limits", json={**limits, "max_windows": 8})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "RUN_BUSY"


def test_budget_exhaustion_can_be_extended_without_fork_and_replay_stays_equal(tmp_path):
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        created = client.post("/api/runs", json={
            "preset_id": "letters", "provider": "fixture", "limits": {"max_calls": 1},
        }).json()
        path = f"/api/runs/{created['run']['id']}"
        assert client.post(path + "/step").status_code == 422
        exhausted = client.get(path).json()
        assert exhausted["run"]["status"] == "budget_exhausted"
        edited = client.patch(path + "/limits", json={**exhausted["run"]["limits"], "max_calls": 24})
        assert edited.status_code == 200
        assert edited.json()["run"]["status"] == "paused" and edited.json()["run"]["last_error"] is None
        resumed = client.post(path + "/step")
        assert resumed.status_code == 200 and resumed.json()["run"]["usage"]["windows"] == 1
        state = app.state.store.load(created["run"]["id"])
        assert verify_replay(state, app.state.store.read_events(state.run.id))["ok"]
