"""Immutable object versions and scope-first resolution for policy execution."""

from __future__ import annotations

from copy import deepcopy

from .kernel import KernelError, fingerprint
from .policy_types import ObjectRef
from .reflection_types import ObjectVersion


def versioned_object(value: dict) -> dict:
    """Version the complete value; preview fields never participate in source identity."""
    row = deepcopy(value)
    row.pop("version", None)
    row.pop("preview_truncated", None)
    row.pop("full_length", None)
    row["version"] = fingerprint(row)
    return row


def archive_objects(existing: list[ObjectVersion], objects: list[dict]) -> list[ObjectVersion]:
    versions = {(item.object_id, item.version): item for item in existing}
    for value in objects:
        row = versioned_object(value)
        key = (row["id"], row["version"])
        record = ObjectVersion(
            object_id=row["id"], version=row["version"], value=row,
            private_scope=row.get("private_scope"),
        )
        if key in versions and versions[key] != record:
            raise KernelError("OBJECT_VERSION_CONFLICT", "不可变对象版本发生冲突。")
        versions[key] = record
    return sorted(versions.values(), key=lambda item: (item.object_id, item.version))


class ObjectResolver:
    """All inputs have already passed permission projection; archives repeat the check."""

    def __init__(self, objects: dict[str, dict], versions: list[ObjectVersion], scope: str):
        self.scope = scope
        self.objects = {
            key: versioned_object(value) for key, value in objects.items()
            if value.get("private_scope") in (None, scope)
        }
        self.versions = {
            (item.object_id, item.version): item.value for item in versions
            if item.private_scope in (None, scope)
        }

    def resolve(self, ref: ObjectRef) -> tuple[dict, ObjectRef]:
        row = self.objects.get(ref.object_id)
        if ref.mode == "version" and (row is None or row["version"] != ref.version):
            row = self.versions.get((ref.object_id, ref.version))
        if row is None or row.get("private_scope") not in (None, self.scope):
            raise KernelError("METHOD_INPUT_UNAVAILABLE", "当前方法所需材料或精确版本在此位置不可见。")
        frozen = ObjectRef(object_id=ref.object_id, mode="version", version=row["version"],
                           start=ref.start, end=ref.end)
        return deepcopy(row), frozen
