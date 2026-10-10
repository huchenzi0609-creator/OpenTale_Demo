"""Bundled graph seeds remain visible across profiles without mutating user presets."""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentale.api import create_app
from opentale.config import _ENV_FIELDS, Settings
from opentale.growth_presets import GrowthPresetLibrary
from opentale.kernel import KernelError

BUNDLES = Path(__file__).resolve().parents[1] / "examples" / "growth-presets"
EXAMPLE_IDS = (
    "a_pale_view_of_hills_summer_opening",
    "baiquan_buxuyao_anke_growth_reading",
)
KEY = "offline-example-dummy-credential"
DEFINITION_FIELDS = ("id", "name", "windows", "shared_seed")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for variable in _ENV_FIELDS:
        monkeypatch.delenv(variable, raising=False)


@contextmanager
def profile(path):
    app = create_app(path, path / "data")
    try:
        with TestClient(app) as client:
            yield app, client
    finally:
        app.state.growth_runner.store.close()


def definition(preset):
    return {key: preset[key] for key in DEFINITION_FIELDS}


def native_definition(key):
    directory = BUNDLES / key
    manifest = json.loads((directory / "preset.json").read_text(encoding="utf-8"))
    windows = []
    for row in manifest["windows"]:
        folder = directory / "windows" / row["id"]
        assert json.loads((folder / "window.json").read_text(encoding="utf-8")) == row
        windows.append({**row, "seed": (folder / "story.md").read_text(encoding="utf-8"),
                        "direction": (folder / "orientation.md").read_text(encoding="utf-8")})
    return {"id": manifest["id"], "name": manifest["name"], "windows": windows,
            "shared_seed": (directory / "shared.md").read_text(encoding="utf-8")}


def small_bundle(root, key="small_example", seed="她把门打开。"):
    directory = root / key
    folder = directory / "windows" / "door"
    folder.mkdir(parents=True)
    row = {"id": "door", "label": "门前"}
    (directory / "preset.json").write_text(json.dumps({"id": key, "name": "内置小例子",
                                                       "windows": [row]}, ensure_ascii=False), encoding="utf-8")
    (directory / "shared.md").write_text("# 共同入口\n一杯温水。\n", encoding="utf-8")
    (folder / "window.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    (folder / "story.md").write_text(seed, encoding="utf-8")
    (folder / "orientation.md").write_text("接住眼下的回应。", encoding="utf-8")
    return directory


def test_native_examples_visible_with_exact_material_in_two_fresh_profiles(tmp_path):
    observed = []
    for number in (1, 2):
        with profile(tmp_path / f"profile-{number}") as (app, client):
            response = client.get("/api/graph-presets")
            assert response.status_code == 200, response.text
            catalog = {preset["id"]: preset for preset in response.json()["presets"]}
            assert set(EXAMPLE_IDS) <= catalog.keys()
            values = {}
            for key in EXAMPLE_IDS:
                response = client.get(f"/api/graph-presets/{key}")
                assert response.status_code == 200, response.text
                preset = response.json()
                expected = native_definition(key)
                assert definition(preset) == expected
                assert preset["origin"] == "builtin"
                assert preset == catalog[key]
                expected_version = hashlib.sha256(
                    json.dumps(expected, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                assert preset["version"] == expected_version
                assert not (app.state.graph_presets.root / key).exists()
                assert not (BUNDLES / key / "current.json").exists()
                assert not (BUNDLES / key / "versions").exists()
                values[key] = preset
            observed.append(values)
    assert observed[0] == observed[1]


@pytest.mark.parametrize("key", EXAMPLE_IDS)
def test_native_example_creates_exact_seed_nodes_without_model_calls(tmp_path, key):
    expected = native_definition(key)
    with profile(tmp_path / "profile") as (_, client):
        response = client.post("/api/graph-runs", json={"provider": "fixture", "preset_id": f"graph:{key}"})
        assert response.status_code == 200, response.text
        snapshot = response.json()
        assert snapshot["run"]["mode"] == "growth_reading"
        assert snapshot["run"]["usage"]["calls"] == 0
        assert snapshot["run"]["usage"]["input_tokens"] == 0
        assert snapshot["run"]["usage"]["output_tokens"] == 0
        assert snapshot["active_scopes"] == []
        windows = {window["id"]: window for window in snapshot["windows"]}
        assert windows.keys() == {window["id"] for window in expected["windows"]}
        shared_ids = set()
        for source in expected["windows"]:
            window = windows[source["id"]]
            assert window["label"] == source["label"]
            assert window["direction"] == source["direction"]
            assert len(window["entry_node_ids"]) == 2
            seed_id, shared_id = window["entry_node_ids"]
            shared_ids.add(shared_id)
            response = client.get(f"/api/graph-runs/{snapshot['run']['id']}/nodes/{seed_id}")
            assert response.status_code == 200, response.text
            assert response.json()["text"] == source["seed"]
        assert len(shared_ids) == 1
        response = client.get(f"/api/graph-runs/{snapshot['run']['id']}/nodes/{shared_ids.pop()}")
        assert response.status_code == 200, response.text
        assert response.json()["text"] == expected["shared_seed"]


def test_builtin_is_read_only_but_edited_copy_persists_with_version_conflicts(tmp_path):
    path = tmp_path / "profile"
    key = EXAMPLE_IDS[0]
    with profile(path) as (app, client):
        original = client.get(f"/api/graph-presets/{key}").json()
        for payload in (definition(original), {**definition(original), "expected_version": original["version"]}):
            response = client.post("/api/graph-presets", json=payload)
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "PRESET_READ_ONLY"
        copied = {**definition(original), "id": "my_example_copy", "name": "我的另一稿",
                  "windows": [{**window, "direction": "从这一稿继续。"} for window in original["windows"]]}
        response = client.post("/api/graph-presets", json=copied)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["origin"] == "custom" and definition(saved) == copied
        assert client.get(f"/api/graph-presets/{key}").json() == original
        assert not (app.state.graph_presets.root / key).exists()
        assert client.get("/api/graph-runs").json()["items"] == []
    with profile(path) as (_, client):
        assert client.get("/api/graph-presets/my_example_copy").json() == saved
        edited = {**copied, "name": "我的第三稿", "expected_version": saved["version"]}
        response = client.post("/api/graph-presets", json=edited)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["version"] != saved["version"]
        response = client.post("/api/graph-presets", json=edited)
        assert response.status_code == 409 and response.json()["error"]["code"] == "PRESET_CONFLICT"
        assert client.get("/api/graph-presets/my_example_copy").json() == updated
        assert client.get(f"/api/graph-presets/{key}").json() == original


def test_existing_custom_id_keeps_priority_and_remains_editable_when_bundle_is_added(tmp_path):
    root, builtin_root = tmp_path / "custom", tmp_path / "bundles"
    custom = {"id": "small_example", "name": "此前保存的稿件", "windows": [
        {"id": "door", "label": "我的门", "seed": "她在这里等过。", "direction": ""},
    ], "shared_seed": "此前的共同入口。"}
    saved = GrowthPresetLibrary(root).save(custom)
    small_bundle(builtin_root)
    library = GrowthPresetLibrary(root, builtins_root=builtin_root)
    assert library.get(custom["id"]) == saved
    assert library.list() == [saved]
    updated = library.save({**custom, "name": "仍可更新的稿件"}, expected_version=saved["version"])
    assert updated["origin"] == "custom" and updated["name"] == "仍可更新的稿件"
    assert updated["version"] != saved["version"]


def test_missing_builtin_directory_does_not_hide_or_prevent_custom_presets(tmp_path):
    library = GrowthPresetLibrary(tmp_path / "custom", builtins_root=tmp_path / "absent")
    custom = {"id": "own_seed", "name": "我的种子", "windows": [
        {"id": "door", "label": "门前", "seed": "她停在门前。", "direction": ""},
    ], "shared_seed": ""}
    saved = library.save(custom)
    assert library.get(custom["id"]) == saved
    assert library.list() == [saved]
    with pytest.raises(KernelError) as captured:
        library.get("missing")
    assert captured.value.code == "PRESET_NOT_FOUND"


def test_builtin_save_rejects_symlink_custom_group_without_writing_outside(tmp_path):
    builtin_root = tmp_path / "bundles"
    small_bundle(builtin_root)
    library = GrowthPresetLibrary(tmp_path / "custom", builtins_root=builtin_root)
    draft = definition(library.get("small_example"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (library.root / "small_example").symlink_to(outside, target_is_directory=True)
    with pytest.raises(KernelError) as captured:
        library.save(draft)
    assert captured.value.code == "PRESET_INVALID"
    assert not list(outside.iterdir())


@pytest.mark.parametrize("part", ["preset.json", "shared.md", "windows/door/window.json",
                                 "windows/door/story.md", "windows/door/orientation.md", "windows"])
def test_bundle_reads_reject_symlink_files_and_window_container(tmp_path, part):
    builtin_root = tmp_path / "bundles"
    directory = small_bundle(builtin_root)
    library = GrowthPresetLibrary(tmp_path / "custom", builtins_root=builtin_root)
    assert library.get("small_example")["origin"] == "builtin"
    original = directory / part
    target = tmp_path / "outside"
    if original.is_dir():
        shutil.move(str(original), target)
        original.symlink_to(target, target_is_directory=True)
    else:
        target.write_text(KEY, encoding="utf-8")
        original.unlink()
        original.symlink_to(target)
    with pytest.raises(KernelError) as captured:
        library.get("small_example")
    assert captured.value.code == "PRESET_INVALID" and KEY not in str(captured.value)
    assert library.list() == []


def test_builtin_api_reads_and_creation_recheck_reloaded_credential(tmp_path):
    builtin_root = tmp_path / "bundles"
    small_bundle(builtin_root, seed=KEY)
    with profile(tmp_path / "profile") as (app, client):
        app.state.graph_presets.builtins_root = builtin_root
        assert client.get("/api/graph-presets/small_example").status_code == 200
        app.state.growth_runner.settings = Settings(api_key=KEY)
        catalog = client.get("/api/graph-presets")
        assert catalog.status_code == 200 and catalog.json()["presets"] == []
        assert KEY not in catalog.text
        for response in (client.get("/api/graph-presets/small_example"),
                         client.post("/api/graph-runs", json={"provider": "fixture", "preset_id": "graph:small_example"})):
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "CONTENT_UNSAFE"
            assert KEY not in response.text
        assert client.get("/api/graph-runs").json()["items"] == []
