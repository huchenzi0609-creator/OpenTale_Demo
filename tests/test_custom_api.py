"""Custom authoring -> durable structured files -> actual offline execution."""

import json

from fastapi.testclient import TestClient
from opentale.api import create_app


def definition(count=3):
    return {
        "id": "my_experiment",
        "name": "河岸上的几种回忆",
        "description": "自定义私有实验",
        "positions": [
            {
                "id": f"person_{n}",
                "name": f"叙述者{n}",
                "seed": f"# 叙述者{n}\n\n他记得河边的一件小事，但有时觉得那是别人的经历。",
            }
            for n in range(count)
        ],
        "expressions": [
            {"id": "opening_1", "text": "雨停了，河边留下一个没有人认领的脚印。", "role": "往事与当下"}
        ],
    }


def test_custom_editor_lifecycle_and_saved_runs_stay_independent(tmp_path):
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        payload = definition()
        saved = client.post("/api/presets", json=payload)
        assert saved.status_code == 200
        preset = saved.json()["preset"]
        assert preset["origin"] == "custom" and preset["editable"]
        assert preset["version"]
        from pathlib import Path

        directory = Path(preset["storage_path"])
        assert directory.is_relative_to(tmp_path / "data" / "presets")
        assert json.loads((directory / "preset.json").read_text())["positions"][0] == {
            "id": "person_0",
            "name": "叙述者0",
        }
        assert (directory / "characters" / "person_0" / "character.md").read_text() == payload["positions"][
            0
        ]["seed"]
        assert json.loads((directory / "characters" / "person_0" / "character.json").read_text()) == {
            "id": "person_0",
            "name": "叙述者0",
        }
        assert saved.json()["preset"] in client.get("/api/presets").json()["presets"]
        created = client.post("/api/runs", json={"preset_id": preset["id"], "provider": "fixture"}).json()
        run_id = created["run"]["id"]
        stepped = client.post(f"/api/runs/{run_id}/step")
        assert stepped.status_code == 200
        original = stepped.json()
        assert original["story"] and len(original["audit"]["joint_targets"]) == 3
        assert all(
            use["role"] == "往事与当下" for use in original["uses"] if use["expression_id"] == "opening_1"
        )
        assert client.get(f"/api/runs/{run_id}/replay").json()["ok"]
        ablation = client.post(
            f"/api/runs/{run_id}/counterfactual",
            json={"target_position_id": "person_0", "source_position_id": "person_1"},
        ).json()
        assert ablation["structural_effect"] and ablation["semantic_fixture_effect"]
        edited = definition(4)
        edited["positions"][0]["seed"] = "# 新的起点\n她认出了另外一种过去。"
        updated = client.put(
            "/api/presets/my_experiment", json={**edited, "expected_version": preset["version"]}
        )
        assert updated.status_code == 200 and updated.json()["preset"]["version"] != preset["version"]
        assert client.get(f"/api/runs/{run_id}").json() == original
        assert (
            client.put(
                "/api/presets/my_experiment", json={**edited, "expected_version": preset["version"]}
            ).status_code
            == 409
        )
        assert client.post("/api/presets", json=payload).status_code == 409
        new = client.post("/api/runs", json={"preset_id": preset["id"], "provider": "fixture"}).json()
        assert len(new["positions"]) == 4 and new["positions"][0]["seed"] == edited["positions"][0]["seed"]
        assert client.post(f"/api/runs/{new['run']['id']}/step").status_code == 200
    with TestClient(create_app(tmp_path, tmp_path / "data")) as restarted:
        current = next(
            p for p in restarted.get("/api/presets").json()["presets"] if p["id"] == "my_experiment"
        )
        assert current["positions"][0]["seed"] == edited["positions"][0]["seed"]


def test_long_markdown_and_large_unicode_request_are_valid(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        payload = definition(8)
        for position in payload["positions"]:
            position["seed"] = "记忆中的河岸。" * 500
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        assert len(encoded) > 65536
        saved = client.post("/api/presets", content=encoded, headers={"Content-Type": "application/json"})
        assert saved.status_code == 200
        run = client.post("/api/runs", json={"preset_id": payload["id"], "provider": "fixture"})
        assert run.status_code == 200 and len(run.json()["positions"][0]["seed"]) > 1600
        assert client.post(f"/api/runs/{run.json()['run']['id']}/step").status_code == 200


def test_readonly_invalid_and_credential_inputs_are_safe(tmp_path):
    sentinel = "opentale-fake-credential-sentinel"
    (tmp_path / ".env.local").write_text(f"OPENTALE_API_KEY={sentinel}\n")
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        builtins = client.get("/api/presets").json()["presets"]
        assert all(p["editable"] is False for p in builtins)
        invalid = definition()
        invalid["id"] = "../escape"
        assert client.post("/api/presets", json=invalid).status_code == 422
        invalid = definition()
        invalid["positions"] = invalid["positions"][:2]
        assert client.post("/api/presets", json=invalid).status_code == 422
        duplicate = definition()
        duplicate["positions"][1]["id"] = duplicate["positions"][0]["id"]
        assert client.post("/api/presets", json=duplicate).status_code == 422
        bad = definition()
        bad["positions"][0]["seed"] = f"这不是故事材料：{sentinel}"
        result = client.post("/api/presets", json=bad)
        assert result.status_code == 422 and sentinel not in result.text
        assert not list((tmp_path / "data" / "presets").glob("my_experiment/versions/*"))
        readonly = definition()
        readonly["id"] = "letters"
        assert (
            client.put("/api/presets/letters", json={**readonly, "expected_version": "a" * 32}).status_code
            == 409
        )
        assert (
            client.post(
                "/api/presets", json=definition(), headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/presets", content=b"x" * 1048577).status_code == 413
        assert client.get("/api/presets").json()["presets"] == builtins
