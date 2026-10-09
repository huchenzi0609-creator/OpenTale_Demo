"""Opt-in graph routes remain isolated from live provider configuration."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from opentale.api import create_app
from opentale.config import _ENV_FIELDS
from opentale.growth_kernel import add_link

from scripts.verify_growth import all_rows, verify, verify_privacy

WINDOWS = [
    {"id": "door", "label": "门前", "seed": "她把手放到门闩上。", "direction": "展开门前的回应"},
    {"id": "stairs", "label": "台阶", "seed": "他停在台阶下。", "direction": "接住另一个方向"},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    for variable in _ENV_FIELDS:
        monkeypatch.delenv(variable, raising=False)
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as connection:
        yield connection
    app.state.growth_runner.store.close()


def create(client, **changes):
    response = client.post("/api/graph-runs", json={"provider": "fixture", "title": "离线图实验",
                           "windows": WINDOWS, "shared_seed": "门前放着一杯温水。", **changes})
    assert response.status_code == 200, response.text
    return response.json()


def pages(client, path, *, limit=2):
    offset, seen = 0, set()
    collected = []
    for _ in range(200):
        assert offset not in seen
        seen.add(offset)
        response = client.get(path, params={"offset": offset, "limit": limit})
        assert response.status_code == 200, response.text
        page = response.json()
        collected.extend(page["items"])
        next_offset = page["next_offset"]
        if next_offset is None:
            return collected
        assert next_offset > offset
        offset = next_offset
    raise AssertionError("Pagination did not terminate")


def test_create_shared_entry_and_budget_defaults_are_explicitly_unlimited(client):
    snapshot = create(client)
    run_id = snapshot["run"]["id"]
    assert snapshot["schema_version"] == 6
    assert snapshot["run"]["mode"] == "growth_reading"
    for field in ("max_calls", "max_tokens", "max_windows"):
        assert snapshot["run"]["limits"][field] is None
    assert len(snapshot["nodes"]) == 3
    assert len(set.intersection(*(set(window["entry_node_ids"]) for window in snapshot["windows"]))) == 1
    assert client.get(f"/api/graph-runs/{run_id}/status").json()["graph_commit"] == 3
    assert client.get("/api/runs").json()["runs"] == []
    assert client.get("/api/graph-runs").json()["items"][0]["id"] == run_id


def test_growth_actions_actual_method_chain_and_independent_readers_same_graph(client):
    snapshot = create(client)
    path = f"/api/graph-runs/{snapshot['run']['id']}"
    for _ in range(22):
        response = client.post(path + "/growth/step")
        assert response.status_code == 200, response.text
        snapshot = response.json()
    for window in snapshot["windows"]:
        scope = f"window:{window['id']}"
        receipts = [row for row in pages(client, path + "/receipts") if row["scope_id"] == scope]
        assert [row["action"] for row in receipts[:9]] == [
            "inspect", "apply", "try_write", "respond", "revise", "adopt", "apply", "try_write", "publish",
        ]
        trials = [trial for trial in snapshot["trials"] if trial["scope_id"] == scope]
        assert trials[0]["method_ref"] != trials[1]["method_ref"]
        assert trials[0]["feedback_refs"] and trials[1]["adopted"]
    commit = snapshot["graph_commit"]
    first_entry, second_entry = [window["entry_node_ids"][0] for window in snapshot["windows"]]
    snapshot = client.post(path + "/readings", json={"title": "甲读轨", "start_node_id": first_entry}).json()
    first_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "甲读轨")
    snapshot = client.post(path + "/readings", json={"title": "乙读轨", "entry_node_ids": [second_entry]}).json()
    second_id = next(track["id"] for track in snapshot["readings"] if track["title"] == "乙读轨")
    for _ in range(6):
        response = client.post(path + f"/readings/{first_id}/step")
        assert response.status_code == 200, response.text
    first_state = response.json()["readings"]
    first_before = next(track for track in first_state if track["id"] == first_id)
    for _ in range(3):
        response = client.post(path + f"/readings/{second_id}/step")
        assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["graph_commit"] == commit
    first = next(track for track in snapshot["readings"] if track["id"] == first_id)
    second = next(track for track in snapshot["readings"] if track["id"] == second_id)
    assert first == first_before
    assert first["visits"][0]["node_id"] == first_entry
    assert second["visits"][0]["node_id"] == second_entry
    assert len(first["passages"]) == 2 and len(second["passages"]) == 1
    for track_id in (first_id, second_id):
        visits = pages(client, path + f"/readings/{track_id}/visits", limit=1)
        passages = pages(client, path + f"/readings/{track_id}/passages", limit=1)
        assert all(row["track_id"] == track_id for row in visits + passages)


def test_exact_node_version_returns_original_markdown_and_unknown_version_is_safe(client):
    snapshot = create(client)
    path = f"/api/graph-runs/{snapshot['run']['id']}"
    node = snapshot["nodes"][0]
    result = client.get(path + f"/nodes/{node['id']}", params={"version": node["version"]})
    assert result.status_code == 200
    assert result.json()["node"]["version"] == node["version"]
    assert result.json()["text"] == WINDOWS[0]["seed"]
    unknown = client.get(path + f"/nodes/{node['id']}", params={"version": "unoffered-version"})
    assert unknown.status_code == 404
    assert "unoffered-version" not in unknown.text


def test_small_graph_pages_cover_all_nodes_and_denser_association_links(client):
    snapshot = create(client)
    run_id = snapshot["run"]["id"]
    runner = client.app.state.growth_runner
    node_ids = [node["id"] for node in snapshot["nodes"]]
    state = runner.load(run_id)
    for index, (source, target) in enumerate((a, b) for a in node_ids for b in node_ids if a != b):
        state = add_link(runner.store, state, source, target, scope_id="window:door",
                         request_id=f"association-{index}", relation="共同入口中的关联")
    expected = runner.snapshot(run_id)
    offset, visited_offsets, nodes, links = 0, set(), [], []
    for _ in range(30):
        assert offset not in visited_offsets
        visited_offsets.add(offset)
        response = client.get(f"/api/graph-runs/{run_id}/graph", params={"offset": offset, "limit": 2})
        assert response.status_code == 200
        page = response.json()
        nodes.extend(page["nodes"])
        links.extend(page["links"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    else:
        raise AssertionError("Graph pagination did not terminate")
    assert {node["id"] for node in nodes} == {node["id"] for node in expected["nodes"]}
    assert {link["id"] for link in links} == {link["id"] for link in expected["links"]}


@pytest.mark.parametrize("endpoint", ["", "/status", "/graph", "/methods", "/nodes/missing"])
def test_missing_graph_read_endpoints_return_safe_404(client, endpoint):
    response = client.get("/api/graph-runs/missing" + endpoint)
    assert response.status_code == 404
    assert "Traceback" not in response.text


@pytest.mark.parametrize("path,body", [
    ("/api/graph-runs", {"provider": "fixture"}),
    ("/api/graph-presets", {"id": "outside", "name": "来自外部", "windows": WINDOWS}),
])
def test_cross_origin_graph_writes_are_rejected(client, path, body):
    response = client.post(path, json=body, headers={"Origin": "https://outside.example"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_REJECTED"


def test_invalid_and_missing_key_requests_do_not_echo_secret_fields_or_call_live_model(client):
    sentinel = "dummy-secret-field-never-echo"
    invalid = client.post("/api/graph-runs", json={"provider": "fixture", "api_key": sentinel})
    assert invalid.status_code == 422 and sentinel not in invalid.text
    live = client.post("/api/graph-runs", json={"provider": "deepseek"})
    assert live.status_code == 503 and "API_KEY_MISSING" in live.text
    assert client.get("/api/graph-runs").json()["items"] == []


def test_pagination_bounds_are_safe_request_errors(client):
    snapshot = create(client)
    path = f"/api/graph-runs/{snapshot['run']['id']}"
    for endpoint in ("/graph", "/methods"):
        assert client.get(path + endpoint, params={"offset": -1}).status_code == 422
        assert client.get(path + endpoint, params={"limit": 0}).status_code == 422
        assert client.get(path + endpoint, params={"limit": 101}).status_code == 422


def test_shared_verification_checks_actual_scope_privacy_and_indexed_pages(client):
    snapshot = create(client)
    runner = client.app.state.growth_runner
    run_id = snapshot["run"]["id"]
    privacy = verify_privacy(runner, run_id)
    assert all(privacy.values())
    rows = all_rows(runner.store, run_id, "node", owner=True, page_size=1)
    assert len(rows) == runner.store.count_objects(run_id, "node")
    assert len({row["id"] for row in rows}) == len(rows)


async def test_offline_report_executes_kernel_replay_and_distinguishes_literary_limits(tmp_path):
    report = await verify(18, 6, 2, tmp_path / "verification")
    assert report["ok"] and report["provider"] == "fixture"
    assert len(report["method_chains"]) == len(report["readings"]) == 2
    assert report["criteria_change"]["changed_selection"]
    assert report["criteria_change"]["old_ref"] != report["criteria_change"]["new_ref"]
    assert report["object_integrity"]["full_object_history"]
    assert report["replay"]["operational_replay"] and report["replay"]["current_head_matches"]
    assert not report["replay"]["semantic_replay"]
    assert all(report["privacy"].values())
    assert report["usage"]["input_tokens"] == report["usage"]["output_tokens"] == 0
