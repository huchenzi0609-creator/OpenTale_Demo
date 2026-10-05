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


def test_configuration_diagnostics_follow_reload_without_exposing_values(tmp_path, monkeypatch):
    for field in (
        "OPENTALE_API_KEY", "OPENTALE_BASE_URL", "OPENTALE_MODEL", "OPENTALE_TIMEOUT_SECONDS",
        "OPENTALE_MAX_OUTPUT_TOKENS", "OPENTALE_PORT",
    ):
        monkeypatch.delenv(field, raising=False)
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        initial = client.get("/api/config").json()
        assert initial["diagnostics"]["config_file_path"] == str(tmp_path / ".env.local")
        assert not initial["diagnostics"]["config_file_exists"]
        assert initial["diagnostics"]["credential_source"] == "none"

        credential = "fixture-configuration-sentinel"
        file = tmp_path / ".env.local"
        file.write_text(f"OPENTALE_API_KEY={credential}\nOPENTALE_BASE_URL=https://example.test\n")
        # GET describes the active snapshot; only explicit reload adopts the file.
        assert not client.get("/api/config").json()["configured"]
        loaded = client.post("/api/config/reload")
        assert loaded.status_code == 200
        current = loaded.json()
        assert current["configured"]
        assert current["diagnostics"]["file_api_key_present"]
        assert current["diagnostics"]["credential_source"] == "file"
        assert credential not in loaded.text and "example.test" not in loaded.text

        file.write_text(f"OPENTALE_API_KEY={credential}\nOPENTALE_MODEL=invalid model\n")
        invalid = client.post("/api/config/reload")
        assert invalid.status_code == 422 and credential not in invalid.text
        assert client.get("/api/config").json() == current

        file.write_text(f"OPENTALE_API_KEY={credential}\n")
        monkeypatch.setenv("OPENTALE_API_KEY", "")
        overridden = client.post("/api/config/reload")
        assert overridden.status_code == 200
        metadata = overridden.json()
        assert not metadata["configured"]
        assert metadata["diagnostics"]["file_api_key_present"]
        assert metadata["diagnostics"]["environment_api_key_empty_override"]
        assert metadata["diagnostics"]["credential_source"] == "environment"
        assert credential not in overridden.text


def test_recovery_fork_accepts_bounded_budget_without_mutating_parent(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        created = client.post("/api/runs", json={"preset_id": "letters", "provider": "fixture"}).json()
        path = f"/api/runs/{created['run']['id']}"
        limits = {"max_calls": 16, "max_tokens": 120000, "max_parallel": 1, "max_windows": 1}
        response = client.post(path + "/fork", json={"title": "恢复分支", "limits": limits})
        assert response.status_code == 200 and response.json()["run"]["limits"] == limits
        assert client.get(path).json() == created
        before = len(client.get("/api/runs").json()["runs"])
        invalid = client.post(path + "/fork", json={"limits": {**limits, "max_parallel": 99}})
        assert invalid.status_code == 422
        assert len(client.get("/api/runs").json()["runs"]) == before
