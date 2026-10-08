"""Versioned preset library; Markdown seeds become live, mutable initial uses.

Custom revisions are immutable directories. A single atomic pointer replacement
publishes a completely prepared revision; optimistic versions prevent lost edits.
Descriptor-relative, no-follow filesystem operations keep links out of the library.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from .domain import Binding, Continuation, Event, Expression, Limits, Position, Record, Run, Snapshot, Use
from .kernel import audit_for, frame_for, use_id

_PRESET_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_POSITION_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
_VERSION = re.compile(r"[a-f0-9]{32}")
_MAX_FILE_BYTES = 512 * 1024


class InitialExpression(Record):
    id: str
    text: str = Field(min_length=1, max_length=6000)
    role: str = Field(default="共同生活", min_length=1, max_length=80)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _PRESET_ID.fullmatch(value) or value.startswith(("seed-", "expr-")):
            raise ValueError("共享表达标识无效或使用了保留前缀。")
        return value

    @field_validator("text", "role")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容不能为空。")
        return value

    @field_validator("role", mode="before")
    @classmethod
    def trim_role(cls, value):
        return value.strip() if isinstance(value, str) else value


class PresetDefinition(Record):
    id: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    positions: list[Position] = Field(min_length=3, max_length=8)
    expressions: list[InitialExpression] = Field(min_length=1, max_length=6)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _PRESET_ID.fullmatch(value):
            raise ValueError("预设标识须为安全的小写 ASCII 标识。")
        return value

    @field_validator("name", mode="before")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if isinstance(value, str):
            value = value.strip()
        if isinstance(value, str) and not value:
            raise ValueError("预设名称不能为空。")
        return value

    @field_validator("positions", mode="before")
    @classmethod
    def trim_position_names(cls, value):
        if not isinstance(value, list):
            return value
        result = []
        for position in value:
            if isinstance(position, dict) and isinstance(position.get("name"), str):
                position = {**position, "name": position["name"].strip()}
            elif isinstance(position, Position):
                position = position.model_copy(update={"name": position.name.strip()})
            result.append(position)
        return result

    @model_validator(mode="after")
    def valid_positions_and_unique_ids(self) -> PresetDefinition:
        texts = [self.name, self.description]
        texts.extend(text for position in self.positions for text in (position.name, position.seed))
        texts.extend(text for expression in self.expressions for text in (expression.text, expression.role))
        try:
            for text in texts:
                text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("预设文本须使用有效的 Unicode 字符。") from None
        for position in self.positions:
            if not _POSITION_ID.fullmatch(position.id):
                raise ValueError("位置标识须为安全的小写 ASCII 标识。")
            if not position.name.strip() or len(position.name) > 80:
                raise ValueError("位置名称须为 1 至 80 个字符。")
            if not position.seed.strip() or len(position.seed) > 12000:
                raise ValueError("人物 Markdown 须为 1 至 12000 个字符。")
        for records in (self.positions, self.expressions):
            identifiers = [record.id for record in records]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("位置与表达各自的标识不得重复。")
        return self


class PresetError(Exception):
    """A safe public error; never contains paths, file contents or exception text."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _invalid() -> PresetError:
    return PresetError("PRESET_INVALID", "预设内容或文件格式无效，请检查字段与人物文件。")


def _filesystem_error(error: OSError) -> PresetError:
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return PresetError("PRESET_PATH_UNSAFE", "预设路径不安全，不允许符号链接或非目录路径。")
    return PresetError("PRESET_STORAGE_ERROR", "预设文件无法安全读取或保存，请检查本地目录权限。")


@contextmanager
def _directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open every ancestor without following symlinks, creating private dirs if requested."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise PresetError("PRESET_PATH_UNSAFE", "预设路径不安全。")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_text(path: Path) -> str:
    with _directory_fd(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PresetError("PRESET_PATH_UNSAFE", "预设文件必须为普通文件。")
            if metadata.st_size > _MAX_FILE_BYTES:
                raise _invalid()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                raise _invalid()
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                raise _invalid() from None
        finally:
            os.close(descriptor)


def _read_json(path: Path) -> dict:
    try:
        result = json.loads(_read_text(path))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(result, dict):
        raise _invalid()
    return result


def _write_private(path: Path, text: str) -> None:
    with _directory_fd(path.parent) as parent:
        descriptor = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(text)
                stream.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)


def _json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _directory_names(root: Path) -> list[str]:
    try:
        with _directory_fd(root) as descriptor:
            result = []
            for name in os.listdir(descriptor):
                mode = os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                    result.append(name)
            return sorted(result)
    except FileNotFoundError:
        return []


def _load_definition(directory: Path, *, custom: bool) -> PresetDefinition:
    data = _read_json(directory / "preset.json")
    if not isinstance(data.get("positions"), list) or not 3 <= len(data["positions"]) <= 8:
        raise _invalid()
    positions = []
    for metadata in data["positions"]:
        if not isinstance(metadata, dict) or set(metadata) != {"id", "name"}:
            raise _invalid()
        position_id = metadata["id"]
        if not isinstance(position_id, str) or not _POSITION_ID.fullmatch(position_id):
            raise _invalid()
        character = directory / "characters" / position_id
        if custom and _read_json(character / "character.json") != metadata:
            raise _invalid()
        positions.append({**metadata, "seed": _read_text(character / "character.md")})
    try:
        return PresetDefinition.model_validate({**data, "positions": positions})
    except ValidationError:
        raise _invalid() from None


class PresetLibrary:
    def __init__(self, builtin_root: Path, custom_root: Path):
        # Configured parents are trusted. Canonicalize OS aliases (/tmp, /var)
        # before no-follow traversal; the library root itself remains unchecked
        # by resolve() so an injected root link is still rejected.
        self.builtin_root = builtin_root.parent.resolve() / builtin_root.name
        self.custom_root = custom_root.parent.resolve() / custom_root.name
        self.errors: list[dict] = []
        self._lock = threading.RLock()

    @staticmethod
    def _check_id(identifier: str) -> None:
        if not _PRESET_ID.fullmatch(identifier):
            raise _invalid()

    @staticmethod
    def _enrich(definition: PresetDefinition, *, directory: Path | None, version: str | None) -> dict:
        result = definition.model_dump()
        result.update(
            origin="custom" if directory else "builtin", editable=directory is not None, version=version
        )
        if directory is not None:
            result["storage_path"] = str(directory.absolute())
        return result

    def _is_builtin(self, identifier: str) -> bool:
        try:
            with _directory_fd(self.builtin_root / identifier):
                return True
        except FileNotFoundError:
            return False

    def _current_version(self, identifier: str) -> str | None:
        try:
            pointer = _read_json(self.custom_root / identifier / "current.json")
        except FileNotFoundError:
            return None
        if set(pointer) != {"version"} or not isinstance(pointer["version"], str):
            raise _invalid()
        if not _VERSION.fullmatch(pointer["version"]):
            raise _invalid()
        return pointer["version"]

    def get(self, identifier: str) -> dict:
        self._check_id(identifier)
        try:
            if self._is_builtin(identifier):
                definition = _load_definition(self.builtin_root / identifier, custom=False)
                if definition.id != identifier:
                    raise _invalid()
                return self._enrich(definition, directory=None, version=None)
            version = self._current_version(identifier)
            if version is None:
                raise PresetError("PRESET_NOT_FOUND", "指定预设不存在。")
            directory = self.custom_root / identifier / "versions" / version
            definition = _load_definition(directory, custom=True)
            if definition.id != identifier:
                raise _invalid()
            return self._enrich(definition, directory=directory, version=version)
        except FileNotFoundError:
            raise _invalid() from None
        except OSError as error:
            raise _filesystem_error(error) from None

    def list(self) -> list[dict]:
        self.errors = []
        result = []
        try:
            builtins = _directory_names(self.builtin_root)
            customs = _directory_names(self.custom_root)
        except OSError as error:
            raise _filesystem_error(error) from None
        for identifier in builtins:
            result.append(self.get(identifier))
        for identifier in customs:
            try:
                self._check_id(identifier)
                if identifier in builtins:
                    raise PresetError("PRESET_CONFLICT", "自定义预设与内置预设标识冲突。")
                result.append(self.get(identifier))
            except PresetError as error:
                self.errors.append(
                    {
                        "id": identifier if _PRESET_ID.fullmatch(identifier) else None,
                        "code": error.code,
                        "message": error.message,
                        "retryable": error.retryable,
                    }
                )
        return result

    @contextmanager
    def _save_lock(self) -> Iterator[None]:
        with self._lock, _directory_fd(self.custom_root, create=True) as descriptor:
            lock = os.open(".presets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise PresetError("PRESET_PATH_UNSAFE", "预设锁文件路径不安全。")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
            finally:
                os.close(lock)

    def _publish(self, group: Path, version: str) -> None:
        temporary = f".current-{uuid4().hex}.tmp"
        _write_private(group / temporary, _json_text({"version": version}))
        with _directory_fd(group) as descriptor:
            try:
                os.replace(temporary, "current.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass

    def save(self, definition: PresetDefinition, expected_version: str | None = None) -> dict:
        try:
            definition = PresetDefinition.model_validate(definition.model_dump())
        except (ValidationError, AttributeError):
            raise _invalid() from None
        if expected_version is not None and not _VERSION.fullmatch(expected_version):
            raise _invalid()
        try:
            if self._is_builtin(definition.id):
                raise PresetError("PRESET_READ_ONLY", "内置预设不可覆盖，请使用新的标识另存为自定义预设。")
            with self._save_lock():
                current = self._current_version(definition.id)
                if expected_version is not None and current is None:
                    raise PresetError("PRESET_NOT_FOUND", "指定预设不存在。")
                if current != expected_version:
                    raise PresetError("PRESET_CONFLICT", "预设已存在或已被其他编辑更新，请刷新后再保存。")
                group = self.custom_root / definition.id
                versions = group / "versions"
                with _directory_fd(versions, create=True):
                    pass
                version = uuid4().hex
                with _directory_fd(versions) as descriptor:
                    os.mkdir(version, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                directory = versions / version
                manifest = definition.model_dump()
                manifest["positions"] = []
                for position in definition.positions:
                    metadata = {"id": position.id, "name": position.name}
                    manifest["positions"].append(metadata)
                    character = directory / "characters" / position.id
                    with _directory_fd(character, create=True):
                        pass
                    _write_private(character / "character.json", _json_text(metadata))
                    _write_private(character / "character.md", position.seed)
                _write_private(directory / "preset.json", _json_text(manifest))
                # Read/validate the complete revision before it can become visible.
                _load_definition(directory, custom=True)
                self._publish(group, version)
                return self._enrich(definition, directory=directory, version=version)
        except OSError as error:
            raise _filesystem_error(error) from None


def read_presets(root: Path) -> list[dict]:
    """Compatibility reader for bundled presets, without creating a runtime directory."""
    library = PresetLibrary(root / "presets", root / "runtime" / "presets")
    try:
        return [library.get(identifier) for identifier in _directory_names(library.builtin_root)]
    except OSError as error:
        raise _filesystem_error(error) from None


def initialize(preset: dict, provider: str, limits: Limits, mode: str = "classic") -> Snapshot:
    from .domain import ReadingTrack
    from .reflection_types import ReflectionState

    positions = [Position.model_validate(p) for p in preset["positions"]]
    state = Snapshot(
        run=Run(id=uuid4().hex, title=preset["name"], provider=provider, limits=limits, mode=mode),
        schema_version=5 if mode == "reflexive_v2" else 3 if mode == "reflexive" else 2,
        reflection=ReflectionState() if mode in {"reflexive", "reflexive_v2"} else None,
        reading_tracks=[ReadingTrack(id="main", title="阅读 1")],
        positions=positions,
        expressions=[],
        uses=[],
        continuations=[],
    )
    for position in positions:
        expression_id = f"seed-{position.id}"
        state.expressions.append(
            Expression(
                id=expression_id, text=position.seed, created_revision=0, source_position_id=position.id
            )
        )
        state.uses.append(
            Use(
                id=use_id(position.id, expression_id, "主体背景"),
                position_id=position.id,
                expression_id=expression_id,
                role="主体背景",
                bindings=[
                    Binding(
                        source_position_id=position.id,
                        source_expression_id=expression_id,
                        value=position.seed,
                        anchor=position.seed,
                        revision=0,
                    )
                ],
                revision=0,
            )
        )
        state.continuations.append(Continuation(position_id=position.id, frame_revision=0))
    for initial in preset["expressions"]:
        role = initial.get("role", "共同生活")
        state.expressions.append(Expression(id=initial["id"], text=initial["text"], created_revision=0))
        for position in positions:
            state.uses.append(
                Use(
                    id=use_id(position.id, initial["id"], role),
                    position_id=position.id,
                    expression_id=initial["id"],
                    role=role,
                    bindings=[
                        Binding(
                            source_position_id=position.id,
                            source_expression_id=initial["id"],
                            value=initial["text"],
                            anchor=initial["text"],
                            revision=0,
                        )
                    ],
                    revision=0,
                )
            )
    for continuation in state.continuations:
        frame = frame_for(state, continuation.position_id)
        continuation.condition_fingerprint = frame.condition_fingerprint
        continuation.dependencies = [u["id"] for u in frame.active_uses]
    state.audit = audit_for(state)
    state.events.append(
        Event(
            id=uuid4().hex,
            type="created",
            revision=0,
            data={
                "preset_id": preset["id"],
                "schema_version": state.schema_version,
                "initial": state.model_dump(exclude={"events"}),
            },
        )
    )
    return state
