from fastapi.testclient import TestClient
from opentale.api import create_app


def test_api_lifecycle_and_secret_free_errors(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        assert client.get("/api/health").json()["ok"]
        assert not client.get("/api/config").json()["configured"]
        created = client.post("/api/runs", json={"preset_id": "letters", "provider": "fixture"}).json()
        path = f"/api/runs/{created['run']['id']}"
        result = client.post(path + "/step")
        assert result.status_code == 200
        state = result.json()
        assert len(state["story"]) == 1 and state["audit"]["joint_targets"]
        assert client.get(path + "/export").json() == state
        assert client.post(path + "/fork", json={}).json()["run"]["fork_of"] == created["run"]["id"]
        assert client.post(
            path + "/counterfactual", json={"target_position_id": "writer", "source_position_id": "keeper"}
        ).json()["structural_effect"]
        assert client.delete(path).json()["deleted"]
        assert client.get(path).status_code == 404
        sensitive = "fake-secret-not-a-credential"
        invalid = client.post(
            "/api/runs", json={"preset_id": "letters", "provider": "fixture", "api_key": sensitive}
        )
        assert invalid.status_code == 422 and sensitive not in invalid.text


def test_cross_origin_write_rejected_and_live_requires_key(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        request = {"preset_id": "letters", "provider": "fixture"}
        assert (
            client.post("/api/runs", json=request, headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        result = client.post("/api/runs", json={**request, "provider": "deepseek"})
        assert result.status_code == 503 and "API_KEY_MISSING" in result.text
        assert client.get("/api/verify").json()["ok"]
