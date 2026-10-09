"""Window Markdown presets preserve narrative bytes and immutable safe versions."""

from __future__ import annotations

import copy
import json
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from opentale import growth_presets
from opentale.api import create_app
from opentale.config import _ENV_FIELDS
from opentale.growth_presets import GrowthPresetLibrary
from opentale.kernel import KernelError
from opentale.presets import PresetError

SECRET = "dummy-preset-secret-never-exposed"
ERRORS = (KernelError, PresetError)


@pytest.fixture
def definition():
    return {"id": "two_doors", "name": "两扇门", "windows": [
        {"id": "door", "label": "门前", "seed": "# 门前\n\n她记得🕯️在门前亮着。\n" * 1200,
         "direction": "# 工作方向\n接住人物眼下的具体回应。\n" * 200},
        {"id": "stairs", "label": "台阶", "seed": "# 台阶\n他在台阶下等着。", "direction": "从台阶下继续生活。"},
    ], "shared_seed": "# 共同入口\n门前放着一杯温水。"}


@pytest.fixture
def library(tmp_path):
    return GrowthPresetLibrary(tmp_path / "presets", SECRET)


def test_long_markdown_orientation_roundtrip_immutable_versions_and_private_modes(library, definition):
    original = library.save(definition)
    assert len(original["version"]) == 64
    assert {key: original[key] for key in definition} == definition
    directory = library.root / definition["id"] / "versions" / original["version"]
    assert (directory / "windows" / "door" / "story.md").read_text() == definition["windows"][0]["seed"]
    assert (directory / "windows" / "door" / "orientation.md").read_text() == definition["windows"][0]["direction"]
    manifest = json.loads((directory / "preset.json").read_text())
    assert all(set(window) == {"id", "label"} for window in manifest["windows"])
    edited = copy.deepcopy(definition)
    edited["windows"][0]["seed"] = "# 另一稿\n她把门推开。"
    updated = library.save(edited, expected_version=original["version"])
    assert updated["version"] != original["version"]
    assert library.get(definition["id"])["windows"][0]["seed"] == edited["windows"][0]["seed"]
    assert (directory / "windows" / "door" / "story.md").read_text() == definition["windows"][0]["seed"]
    for path in [library.root, *library.root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_expected_version_conflicts_and_reopened_library(library, definition):
    original = library.save(definition)
    with pytest.raises(ERRORS) as captured:
        library.save(definition)
    assert captured.value.code == "PRESET_CONFLICT"
    edited = {**definition, "name": "第二稿"}
    current = library.save(edited, expected_version=original["version"])
    reopened = GrowthPresetLibrary(library.root, SECRET)
    assert reopened.get(definition["id"]) == current
    with pytest.raises(ERRORS) as captured:
        reopened.save(definition, expected_version=original["version"])
    assert captured.value.code == "PRESET_CONFLICT"
    assert reopened.get(definition["id"]) == current


def test_two_library_instances_cannot_lose_optimistic_update(library, definition):
    original = library.save(definition)
    second = GrowthPresetLibrary(library.root, SECRET)

    def update(number):
        target = library if number == 1 else second
        try:
            return target.save({**definition, "name": f"并发稿件 {number}"}, expected_version=original["version"])["name"]
        except ERRORS as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, [1, 2]))
    assert results.count("PRESET_CONFLICT") == 1
    assert library.get(definition["id"])["name"] in results


@pytest.mark.parametrize("part", ["preset.json", "shared.md", "windows/door/story.md", "windows/door/orientation.md"])
def test_preset_reads_never_follow_symlink_files(library, definition, tmp_path, part):
    saved = library.save(definition)
    directory = library.root / definition["id"] / "versions" / saved["version"]
    target = tmp_path / "outside-content"
    target.write_text(SECRET)
    original = directory / part
    original.unlink()
    original.symlink_to(target)
    with pytest.raises(ERRORS) as captured:
        library.get(definition["id"])
    assert SECRET not in str(captured.value)


def test_preset_reads_never_follow_windows_container_symlink(library, definition, tmp_path):
    saved = library.save(definition)
    directory = library.root / definition["id"] / "versions" / saved["version"]
    outside = tmp_path / "outside-windows"
    shutil.move(str(directory / "windows"), outside)
    (directory / "windows").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ERRORS):
        library.get(definition["id"])


def test_preset_root_symlink_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ERRORS):
        GrowthPresetLibrary(link, SECRET)


def test_changed_version_body_does_not_masquerade_as_original_hash(library, definition):
    saved = library.save(definition)
    story = library.root / definition["id"] / "versions" / saved["version"] / "windows" / "door" / "story.md"
    story.write_text("被另一个内容替换的正文。")
    with pytest.raises(ERRORS):
        library.get(definition["id"])


def test_partial_version_write_failure_does_not_publish_or_poison_retry(library, definition, monkeypatch):
    original = library.save(definition)
    edited = {**definition, "name": "写入中断后的新稿"}
    write = growth_presets._write_private
    attempts = 0

    def fail_mid_version(path, text):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("controlled test interruption")
        return write(path, text)

    monkeypatch.setattr(growth_presets, "_write_private", fail_mid_version)
    with pytest.raises((*ERRORS, OSError)):
        library.save(edited, expected_version=original["version"])
    assert library.get(definition["id"])["version"] == original["version"]
    monkeypatch.setattr(growth_presets, "_write_private", write)
    saved = library.save(edited, expected_version=original["version"])
    assert saved["name"] == edited["name"]
    assert saved["windows"] == edited["windows"]


def test_saving_identical_payload_reuses_verified_immutable_version(library, definition):
    original = library.save(definition)
    saved = library.save(definition, expected_version=original["version"])
    assert saved == original
    versions = library.root / definition["id"] / "versions"
    assert len([path for path in versions.iterdir() if not path.name.startswith(".")]) == 1


def test_dummy_credential_and_traversal_id_rejected_without_public_echo(library, definition):
    bad = {**definition, "name": SECRET}
    with pytest.raises(ERRORS) as captured:
        library.save(bad)
    assert captured.value.code == "CONTENT_UNSAFE" and SECRET not in str(captured.value)
    for key in ("../outside", "a/b", "..", ""):
        with pytest.raises(ERRORS):
            library.save({**definition, "id": key})


def test_graph_preset_api_long_save_conflict_reload_and_prefixed_import(tmp_path, monkeypatch, definition):
    for variable in _ENV_FIELDS:
        monkeypatch.delenv(variable, raising=False)
    app = create_app(tmp_path, tmp_path / "data")
    with TestClient(app) as client:
        first = client.post("/api/graph-presets", json=definition)
        assert first.status_code == 200, first.text
        original = first.json()
        assert original["windows"] == definition["windows"]
        duplicate = client.post("/api/graph-presets", json=definition)
        assert duplicate.status_code == 409
        edited = copy.deepcopy(definition)
        edited["windows"][0]["direction"] = "采用新的工作方向。"
        update = client.post("/api/graph-presets", json={**edited, "expected_version": original["version"]})
        assert update.status_code == 200, update.text
        stale = client.post("/api/graph-presets", json={**definition, "expected_version": original["version"]})
        assert stale.status_code == 409
        fetched = client.get("/api/graph-presets/" + definition["id"]).json()
        assert fetched["version"] == update.json()["version"]
        created = client.post("/api/graph-runs", json={"provider": "fixture", "preset_id": "graph:" + definition["id"]})
        assert created.status_code == 200, created.text
        snapshot = created.json()
        assert snapshot["run"]["limits"]["max_calls"] is None
        assert snapshot["windows"][0]["direction"] == edited["windows"][0]["direction"]
        seed_id = snapshot["windows"][0]["entry_node_ids"][0]
        body = client.get(f"/api/graph-runs/{snapshot['run']['id']}/nodes/{seed_id}").json()["text"]
        assert body == definition["windows"][0]["seed"]
        listed = client.get("/api/graph-presets").json()["presets"]
        assert [preset["id"] for preset in listed] == [definition["id"]]
    app.state.growth_runner.store.close()
