"""Immutable Markdown window presets with no-follow I/O and atomic publication."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from uuid import uuid4

from .kernel import KernelError
from .presets import PresetError, _directory_fd, _directory_names, _read_json, _read_text, _write_private
from .security import contains_credential


class GrowthPresetLibrary:
    def __init__(self, root: Path, credential="", builtins_root: Path | None = None):
        self.root, self.credential, self.lock = root, credential, threading.RLock()
        self.builtins_root = builtins_root
        try:
            with _directory_fd(root, create=True):
                pass
        except (OSError, PresetError):
            raise KernelError("PRESET_INVALID", "预设路径不安全。") from None

    @staticmethod
    def valid_id(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)

    def group(self, key):
        if not self.valid_id(key):
            raise KernelError("PRESET_INVALID", "预设 ID 无效。")
        return self.root / key

    def validate(self, data):
        if not isinstance(data, dict) or set(data) != {"id", "name", "windows", "shared_seed"}:
            raise KernelError("PRESET_INVALID", "预设格式无效。")
        self.group(data["id"])
        if not isinstance(data["name"], str) or not data["name"].strip() or len(data["name"]) > 120:
            raise KernelError("PRESET_INVALID", "预设名称无效。")
        if not isinstance(data["shared_seed"], str) or not isinstance(data["windows"], list) or not data["windows"]:
            raise KernelError("PRESET_INVALID", "预设至少包含一个窗口。")
        windows = []
        for row in data["windows"]:
            if (not isinstance(row, dict) or not {"id", "label", "seed"} <= row.keys()
                    or row.keys() - {"id", "label", "seed", "direction"} or not self.valid_id(row["id"])
                    or any(not isinstance(row.get(k, ""), str) for k in ("label", "seed", "direction"))):
                raise KernelError("PRESET_INVALID", "窗口资料无效。")
            windows.append({"id": row["id"], "label": row["label"], "seed": row["seed"],
                            "direction": row.get("direction", "")})
        if len({w["id"] for w in windows}) != len(windows):
            raise KernelError("PRESET_INVALID", "窗口 ID 须唯一。")
        result = {**data, "windows": windows}
        if contains_credential(result, self.credential):
            raise KernelError("CONTENT_UNSAFE", "预设中不能包含密钥。")
        return result

    @staticmethod
    def version(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def load_directory(self, directory, key, version=None):
        meta = _read_json(directory / "preset.json")
        if meta.get("id") != key or not isinstance(meta.get("windows"), list):
            raise KernelError("PRESET_INVALID", "预设清单无效。")
        windows = []
        for row in meta["windows"]:
            if not isinstance(row, dict) or not self.valid_id(row.get("id")):
                raise KernelError("PRESET_INVALID", "窗口路径无效。")
            folder = directory / "windows" / row["id"]
            system = _read_json(folder / "window.json")
            if system != row:
                raise KernelError("PRESET_INVALID", "窗口清单不匹配。")
            windows.append({**row, "seed": _read_text(folder / "story.md"),
                            "direction": _read_text(folder / "orientation.md")})
        value = self.validate({"id": meta["id"], "name": meta.get("name"), "windows": windows,
                               "shared_seed": _read_text(directory / "shared.md")})
        computed = self.version(value)
        if version is not None and computed != version:
            raise KernelError("PRESET_INVALID", "预设内容不匹配已保存的版本。")
        return {**value, "version": computed}

    def builtin_exists(self, key):
        self.group(key)
        if self.builtins_root is None:
            return False
        try:
            with _directory_fd(self.builtins_root / key):
                return True
        except FileNotFoundError:
            return False
        except (OSError, PresetError):
            raise KernelError("PRESET_INVALID", "示例预设路径不安全。") from None

    def get(self, key):
        try:
            group = self.group(key)
            # Existing custom data keeps precedence; adding a bundled example
            # must not silently hide a user's already saved preset.
            try:
                with _directory_fd(group) as descriptor:
                    has_custom = "current.json" in os.listdir(descriptor)
            except FileNotFoundError:
                has_custom = False
            if not has_custom and self.builtin_exists(key):
                return {**self.load_directory(self.builtins_root / key, key), "origin": "builtin"}
            current = _read_json(group / "current.json")
            version = current.get("version")
            if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
                raise KernelError("PRESET_INVALID", "预设版本无效。")
            return {**self.load_directory(group / "versions" / version, key, version), "origin": "custom"}
        except FileNotFoundError:
            raise KernelError("PRESET_NOT_FOUND", "生成窗口预设不存在。") from None
        except (OSError, PresetError, TypeError, ValueError):
            raise KernelError("PRESET_INVALID", "预设无法安全加载。") from None

    def list(self):
        result = []
        try:
            names = set(_directory_names(self.root))
        except (OSError, PresetError):
            names = set()
        if self.builtins_root is not None:
            try:
                names.update(_directory_names(self.builtins_root))
            except (OSError, PresetError):
                pass
        for name in sorted(names):
            if self.valid_id(name):
                try:
                    result.append(self.get(name))
                except KernelError:
                    continue
        return result

    def save(self, data, expected_version=None):
        data = self.validate(data)
        if self.builtin_exists(data["id"]):
            try:
                with _directory_fd(self.group(data["id"])) as descriptor:
                    custom_exists = "current.json" in os.listdir(descriptor)
            except FileNotFoundError:
                custom_exists = False
            except (OSError, PresetError):
                raise KernelError("PRESET_INVALID", "预设路径不安全。") from None
            if not custom_exists:
                raise KernelError("PRESET_READ_ONLY", "内置示例请使用新 ID 另存为自己的预设。")
        if expected_version is not None and (not isinstance(expected_version, str)
                                            or not re.fullmatch(r"[0-9a-f]{64}", expected_version)):
            raise KernelError("PRESET_INVALID", "预设版本无效。")
        try:
            with self.lock, _directory_fd(self.root) as root_fd:
                lock = os.open(".graph-presets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                try:
                    if not stat.S_ISREG(os.fstat(lock).st_mode):
                        raise KernelError("PRESET_INVALID", "预设锁路径无效。")
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    group = self.group(data["id"])
                    try:
                        current = self.get(data["id"])
                    except KernelError as error:
                        if error.code != "PRESET_NOT_FOUND":
                            raise
                        current = None
                    if (current["version"] if current else None) != expected_version:
                        raise KernelError("PRESET_CONFLICT", "预设已修改，请重新加载后再保存。")
                    version = self.version(data)
                    versions = group / "versions"
                    with _directory_fd(versions, create=True):
                        pass
                    stage_name = ".stage-" + uuid4().hex
                    with _directory_fd(versions) as versions_fd:
                        os.mkdir(stage_name, mode=0o700, dir_fd=versions_fd)
                    stage = versions / stage_name
                    for window in data["windows"]:
                        folder = stage / "windows" / window["id"]
                        with _directory_fd(folder, create=True):
                            pass
                        _write_private(folder / "story.md", window["seed"])
                        _write_private(folder / "orientation.md", window["direction"])
                        _write_private(folder / "window.json", json.dumps({"id": window["id"],
                                       "label": window["label"]}, ensure_ascii=False))
                    _write_private(stage / "shared.md", data["shared_seed"])
                    _write_private(stage / "preset.json", json.dumps({"id": data["id"], "name": data["name"],
                        "windows": [{"id": w["id"], "label": w["label"]} for w in data["windows"]]}, ensure_ascii=False))
                    self.load_directory(stage, data["id"], version)
                    with _directory_fd(versions) as versions_fd:
                        try:
                            os.rename(stage_name, version, src_dir_fd=versions_fd, dst_dir_fd=versions_fd)
                        except OSError as error:
                            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                                raise
                            self.load_directory(versions / version, data["id"], version)
                        os.fsync(versions_fd)
                    temporary = ".current-" + uuid4().hex
                    _write_private(group / temporary, json.dumps({"version": version}))
                    with _directory_fd(group) as group_fd:
                        os.replace(temporary, "current.json", src_dir_fd=group_fd, dst_dir_fd=group_fd)
                        os.fsync(group_fd)
                    return self.get(data["id"])
                finally:
                    os.close(lock)
        except (OSError, PresetError, TypeError, ValueError):
            raise KernelError("PRESET_INVALID", "预设未完成发布，原版本保持可用。") from None
