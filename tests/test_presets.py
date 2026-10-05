import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from opentale import presets
from opentale.domain import Limits
from opentale.presets import PresetDefinition, PresetError, PresetLibrary, initialize, read_presets
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def definition():
    return PresetDefinition.model_validate(
        {
            "id": "shared_dream",
            "name": "共同的梦",
            "description": "三处不同的生活共同承接一份表达。",
            "positions": [
                {"id": "p_a", "name": "甲", "seed": "# 甲\n她仍记得那张桌子。"},
                {"id": "p_b", "name": "乙", "seed": "# 乙\n他正在写一封尚未写出的信。"},
                {"id": "p_c", "name": "丙", "seed": "# 丙\n这个孩子把来处当作自己的构想。"},
            ],
            "expressions": [
                {"id": "shared-letter", "text": "信尚未写出，却已经被读过。", "role": "彼此的来处"}
            ],
        }
    )


@pytest.fixture
def library(tmp_path):
    return PresetLibrary(ROOT / "presets", tmp_path / "runtime" / "presets")


def test_builtin_compatibility_and_custom_create(library, definition):
    assert [item["id"] for item in read_presets(ROOT)] == ["corridor", "letters"]
    saved = library.save(definition)
    assert saved["origin"] == "custom"
    assert saved["editable"] is True
    assert len(saved["version"]) == 32
    assert library.get(definition.id) == saved
    assert {item["id"] for item in library.list()} == {"corridor", "letters", definition.id}
    directory = Path(saved["storage_path"])
    manifest = json.loads((directory / "preset.json").read_text())
    assert all(set(position) == {"id", "name"} for position in manifest["positions"])
    for position in definition.positions:
        character = directory / "characters" / position.id
        assert (character / "character.md").read_text() == position.seed
        assert json.loads((character / "character.json").read_text()) == {
            "id": position.id,
            "name": position.name,
        }


def test_custom_revisions_are_private_and_immutable(library, definition):
    original = library.save(definition)
    directory = Path(original["storage_path"])
    original_manifest = (directory / "preset.json").read_bytes()
    edited = definition.model_copy(deep=True)
    edited.positions[0].seed = "# 改写\n这是一种新的过去。"
    saved = library.save(edited, expected_version=original["version"])
    assert saved["version"] != original["version"]
    assert library.get(definition.id)["positions"][0]["seed"] == edited.positions[0].seed
    assert (directory / "preset.json").read_bytes() == original_manifest
    assert (directory / "characters" / "p_a" / "character.md").read_text() == definition.positions[0].seed
    custom_root = library.custom_root
    for path in [custom_root, *custom_root.rglob("*")]:
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected


def test_create_conflicts_and_optimistic_update(library, definition):
    original = library.save(definition)
    with pytest.raises(PresetError) as conflict:
        library.save(definition)
    assert conflict.value.code == "PRESET_CONFLICT"
    edited = definition.model_copy(update={"name": "第二稿"})
    current = library.save(edited, expected_version=original["version"])
    with pytest.raises(PresetError) as conflict:
        library.save(definition, expected_version=original["version"])
    assert conflict.value.code == "PRESET_CONFLICT"
    assert library.get(definition.id) == current


def test_concurrent_libraries_cannot_lose_update(library, definition):
    original = library.save(definition)
    second = PresetLibrary(library.builtin_root, library.custom_root)

    def update(number):
        current_library = library if number == 1 else second
        try:
            saved = current_library.save(
                definition.model_copy(update={"name": f"稿件 {number}"}), expected_version=original["version"]
            )
            return saved["name"]
        except PresetError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, [1, 2]))
    assert results.count("PRESET_CONFLICT") == 1
    assert library.get(definition.id)["name"] in results


def test_builtin_is_read_only(library):
    original = library.get("letters")
    manifest = (ROOT / "presets" / "letters" / "preset.json").read_bytes()
    draft = PresetDefinition.model_validate(
        {key: value for key, value in original.items() if key in PresetDefinition.model_fields}
    )
    with pytest.raises(PresetError) as error:
        library.save(draft)
    assert error.value.code == "PRESET_READ_ONLY"
    assert (ROOT / "presets" / "letters" / "preset.json").read_bytes() == manifest
    assert library.get("letters") == original


def test_editing_preset_does_not_change_existing_run(library, definition):
    original = library.save(definition)
    state = initialize(original, "fixture", Limits())
    frozen = state.model_dump()
    definition.positions[0].seed = "# 新过去\n她忘记了所有旧事。"
    definition.expressions[0].text = "桌上已经没有信了。"
    library.save(definition, expected_version=original["version"])
    assert state.model_dump() == frozen
    assert (
        initialize(library.get(definition.id), "fixture", Limits()).positions[0].seed
        != state.positions[0].seed
    )


def test_shared_expression_role_is_used_by_initialization(library, definition):
    state = initialize(library.save(definition), "fixture", Limits())
    shared_uses = [use for use in state.uses if use.expression_id == "shared-letter"]
    assert len(shared_uses) == 3
    assert {use.role for use in shared_uses} == {"彼此的来处"}


@pytest.mark.parametrize("count", [3, 4, 5, 6, 7, 8])
def test_supported_position_counts_and_full_limits(library, definition, count):
    draft = definition.model_dump()
    draft["positions"] = [
        {"id": f"person_{i}", "name": f"人物 {i}", "seed": "文" * 12000} for i in range(count)
    ]
    draft["expressions"] = [{"id": f"shared-{i}", "text": "文" * 6000, "role": "用" * 80} for i in range(6)]
    saved = library.save(PresetDefinition.model_validate(draft))
    assert len(saved["positions"]) == count
    assert len(saved["expressions"]) == 6
    assert library.get(draft["id"])["positions"][0]["seed"] == "文" * 12000
    state = initialize(saved, "fixture", Limits())
    assert len(state.uses) == count * 7
    assert max(len(binding.value) for use in state.uses for binding in use.bindings) == 12000


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../escape"),
        ("id", "Upper"),
        ("id", "中文"),
        ("id", "safe\n"),
        ("id", "a" * 65),
        ("name", " "),
        ("name", "名" * 101),
        ("description", "文" * 2001),
    ],
)
def test_invalid_top_level_fields(definition, field, value):
    draft = definition.model_dump()
    draft[field] = value
    with pytest.raises(ValidationError):
        PresetDefinition.model_validate(draft)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../escape"),
        ("id", "with-hyphen"),
        ("id", "position\n"),
        ("id", "a" * 41),
        ("name", " "),
        ("name", "名" * 81),
        ("seed", "\n\t"),
        ("seed", "文" * 12001),
        ("seed", "\ud800"),
    ],
)
def test_invalid_position_fields(definition, field, value):
    draft = definition.model_dump()
    draft["positions"][0][field] = value
    with pytest.raises(ValidationError):
        PresetDefinition.model_validate(draft)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../../escape"),
        ("id", "expr-new"),
        ("id", "seed-person"),
        ("id", "expression\n"),
        ("text", " "),
        ("text", "文" * 6001),
        ("role", " "),
        ("role", "用" * 81),
    ],
)
def test_invalid_expression_fields(definition, field, value):
    draft = definition.model_dump()
    draft["expressions"][0][field] = value
    with pytest.raises(ValidationError):
        PresetDefinition.model_validate(draft)


@pytest.mark.parametrize(
    "collection,count", [("positions", 2), ("positions", 9), ("expressions", 0), ("expressions", 7)]
)
def test_invalid_collection_counts(definition, collection, count):
    draft = definition.model_dump()
    source = draft[collection][0]
    draft[collection] = [{**source, "id": f"entry_{i}"} for i in range(count)]
    with pytest.raises(ValidationError):
        PresetDefinition.model_validate(draft)


@pytest.mark.parametrize("collection", ["positions", "expressions"])
def test_duplicate_ids_rejected(definition, collection):
    draft = definition.model_dump()
    if collection == "positions":
        draft[collection][1]["id"] = draft[collection][0]["id"]
    else:
        draft[collection].append(draft[collection][0])
    with pytest.raises(ValidationError):
        PresetDefinition.model_validate(draft)


def test_read_and_update_missing_preset_are_safe(library, definition):
    with pytest.raises(PresetError) as error:
        library.get("missing")
    assert error.value.code == "PRESET_NOT_FOUND"
    with pytest.raises(PresetError) as error:
        library.save(definition, expected_version="a" * 32)
    assert error.value.code == "PRESET_NOT_FOUND"
    for identifier in ("../escape", "UPPER", "person\n"):
        with pytest.raises(PresetError) as error:
            library.get(identifier)
        assert error.value.code == "PRESET_INVALID"


def test_malformed_custom_does_not_hide_builtins(library, definition):
    saved = library.save(definition)
    (Path(saved["storage_path"]) / "preset.json").write_text("not valid json", encoding="utf-8")
    assert [item["id"] for item in library.list()] == ["corridor", "letters"]
    assert library.errors == [
        {
            "id": definition.id,
            "code": "PRESET_INVALID",
            "message": "预设内容或文件格式无效，请检查字段与人物文件。",
            "retryable": False,
        }
    ]


@pytest.mark.parametrize("injection", ["../../outside", "not-a-version", "a" * 33])
def test_pointer_cannot_escape_library(library, definition, injection):
    library.save(definition)
    (library.custom_root / definition.id / "current.json").write_text(json.dumps({"version": injection}))
    with pytest.raises(PresetError) as error:
        library.get(definition.id)
    assert error.value.code == "PRESET_INVALID"


@pytest.mark.parametrize(
    "target",
    [
        "root",
        "group",
        "versions",
        "version",
        "characters",
        "profile",
        "markdown",
        "metadata",
        "manifest",
        "pointer",
    ],
)
def test_symlink_traversal_is_rejected(library, definition, tmp_path, target):
    saved = library.save(definition)
    group = library.custom_root / definition.id
    directory = Path(saved["storage_path"])
    character = directory / "characters" / "p_a"
    locations = {
        "root": library.custom_root,
        "group": group,
        "versions": group / "versions",
        "version": directory,
        "characters": directory / "characters",
        "profile": character,
        "markdown": character / "character.md",
        "metadata": character / "character.json",
        "manifest": directory / "preset.json",
        "pointer": group / "current.json",
    }
    location = locations[target]
    outside = tmp_path / "outside"
    if location.is_dir():
        outside.mkdir()
    else:
        outside.write_text("PRIVATE-MARKER-DO-NOT-RETURN")
    location.rename(location.with_name(location.name + "-original"))
    location.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(PresetError) as error:
        library.get(definition.id)
    assert error.value.code == "PRESET_PATH_UNSAFE"
    assert "PRIVATE-MARKER" not in error.value.message
    assert str(outside) not in error.value.message
    if target in {"root", "group", "versions", "pointer"}:
        with pytest.raises(PresetError) as write_error:
            library.save(definition, expected_version=saved["version"])
        assert write_error.value.code == "PRESET_PATH_UNSAFE"


def test_partial_write_failure_never_changes_current(library, definition, monkeypatch):
    original = library.save(definition)
    old_pointer = (library.custom_root / definition.id / "current.json").read_bytes()
    actual_write = presets._write_private
    calls = 0

    def fail_during_preparation(path, text):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("private filesystem detail that must not escape")
        actual_write(path, text)

    monkeypatch.setattr(presets, "_write_private", fail_during_preparation)
    with pytest.raises(PresetError) as error:
        library.save(definition.model_copy(update={"name": "失败稿"}), expected_version=original["version"])
    assert error.value.code == "PRESET_STORAGE_ERROR"
    assert "filesystem detail" not in error.value.message
    assert (library.custom_root / definition.id / "current.json").read_bytes() == old_pointer
    assert library.get(definition.id) == original
    assert len(list((library.custom_root / definition.id / "versions").iterdir())) == 2
    monkeypatch.setattr(presets, "_write_private", actual_write)
    saved = library.save(
        definition.model_copy(update={"name": "恢复稿"}), expected_version=original["version"]
    )
    assert library.get(definition.id)["version"] == saved["version"]


def test_invalid_character_metadata_is_rejected(library, definition):
    saved = library.save(definition)
    metadata = Path(saved["storage_path"]) / "characters" / "p_a" / "character.json"
    metadata.write_text(json.dumps({"id": "p_a", "name": "different"}))
    with pytest.raises(PresetError) as error:
        library.get(definition.id)
    assert error.value.code == "PRESET_INVALID"


def test_trusted_parent_alias_works_but_library_root_link_does_not(tmp_path, definition):
    actual_parent = tmp_path / "real-parent"
    actual_parent.mkdir()
    alias = tmp_path / "trusted-parent-alias"
    alias.symlink_to(actual_parent, target_is_directory=True)
    library = PresetLibrary(ROOT / "presets", alias / "presets")
    saved = library.save(definition)
    assert library.get(definition.id) == saved
    assert len(library.list()) == 3
    assert Path(saved["storage_path"]).is_relative_to(actual_parent)
    malicious_root = tmp_path / "root-link"
    malicious_root.symlink_to(actual_parent / "presets", target_is_directory=True)
    unsafe = PresetLibrary(ROOT / "presets", malicious_root)
    with pytest.raises(PresetError) as error:
        unsafe.get(definition.id)
    assert error.value.code == "PRESET_PATH_UNSAFE"


def test_names_and_roles_trim_without_trimming_markdown(library, definition):
    draft = definition.model_dump()
    draft["name"] = "  实验名称  "
    draft["positions"][0]["name"] = "  甲  "
    draft["positions"][0]["seed"] = "\n# 甲\n正文\n\n"
    draft["expressions"][0]["role"] = "  共同的过去  "
    saved = library.save(PresetDefinition.model_validate(draft))
    assert saved["name"] == "实验名称"
    assert saved["positions"][0]["name"] == "甲"
    assert saved["positions"][0]["seed"] == draft["positions"][0]["seed"]
    assert saved["expressions"][0]["role"] == "共同的过去"


def test_builtin_profile_link_is_rejected_without_reading_destination(tmp_path, definition):
    builtin = tmp_path / "builtin"
    directory = builtin / definition.id
    directory.mkdir(parents=True)
    manifest = definition.model_dump()
    manifest["positions"] = [{"id": position.id, "name": position.name} for position in definition.positions]
    (directory / "preset.json").write_text(json.dumps(manifest))
    for position in definition.positions:
        character = directory / "characters" / position.id
        character.mkdir(parents=True)
        (character / "character.md").write_text(position.seed)
    private = tmp_path / "private.md"
    private.write_text("PRIVATE-MARKER-DO-NOT-RETURN")
    character_md = directory / "characters" / "p_a" / "character.md"
    character_md.unlink()
    character_md.symlink_to(private)
    library = PresetLibrary(builtin, tmp_path / "custom")
    with pytest.raises(PresetError) as error:
        library.get(definition.id)
    assert error.value.code == "PRESET_PATH_UNSAFE"
    assert "PRIVATE-MARKER" not in error.value.message


def test_invalid_utf8_markdown_has_safe_error(library, definition):
    saved = library.save(definition)
    markdown = Path(saved["storage_path"]) / "characters" / "p_a" / "character.md"
    markdown.write_bytes(b"\xff\xfePRIVATE-MARKER")
    with pytest.raises(PresetError) as error:
        library.get(definition.id)
    assert error.value.code == "PRESET_INVALID"
    assert "PRIVATE-MARKER" not in error.value.message
    assert {item["id"] for item in library.list()} == {"corridor", "letters"}


def test_failed_pointer_replace_preserves_previous_revision(library, definition, monkeypatch):
    original = library.save(definition)

    def fail_replace(*args, **kwargs):
        raise OSError("private path detail")

    monkeypatch.setattr(presets.os, "replace", fail_replace)
    with pytest.raises(PresetError) as error:
        library.save(
            definition.model_copy(update={"name": "不可见的稿件"}), expected_version=original["version"]
        )
    assert error.value.code == "PRESET_STORAGE_ERROR"
    assert library.get(definition.id) == original
    assert not list((library.custom_root / definition.id).glob(".current-*.tmp"))
