"""Backed-up, verified recovery of review-only pauses; dry-run is the default.

No provider, Settings, dotenv or credential-bearing configuration is imported.
Run against an idle/stopped service when using --apply. A concurrent change in
the selected run prevents installation; the exclusive backup remains available.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from opentale.growth_kernel import GrowthError
from opentale.growth_recovery import recover_review_pauses
from opentale.growth_replay import replay_operations, verify_objects
from opentale.growth_store import GrowthStore, digest


class RecoveryFailure(Exception):
    """Fixed diagnostic codes; exception bodies never contain stored text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _backup(source: Path, target: Path) -> None:
    """Consistent SQLite snapshot with no overwrite and private permissions."""
    if target.resolve() == source.resolve():
        raise RecoveryFailure("BACKUP_SAME_AS_SOURCE")
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RecoveryFailure("BACKUP_ALREADY_EXISTS") from None
    os.close(descriptor)
    origin = snapshot = None
    try:
        origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(target)
        origin.backup(snapshot)
    except BaseException:
        if snapshot is not None:
            snapshot.close()
            snapshot = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if snapshot is not None:
            snapshot.close()
        if origin is not None:
            origin.close()


def _verify(store: GrowthStore, run_id: str, page_size: int) -> dict:
    cursor, count = 0, 0
    while True:
        receipt = verify_objects(store, run_id, after_seq=cursor, limit=page_size)
        cursor = receipt["through_seq"]
        count += receipt["objects_checked"]
        if not receipt["has_more"]:
            break
    replay = replay_operations(store, run_id, page_size=page_size)
    return {"object_versions_checked": count, "object_cursor": cursor, "object_integrity": True,
            "kernel_replay": True, "operations_replayed": replay.get("operations_replayed", {})}


def _last_object_sequence(store: GrowthStore, run_id: str) -> int:
    # Compare the indexed selected-run revision marker while holding the write
    # transaction; no full body traversal is repeated against the live database.
    row = store._db.execute("SELECT COALESCE(MAX(seq),0) FROM objects WHERE run_id=?", (run_id,)).fetchone()
    return row[0]


def recover_database(database: Path, run_id: str, *, apply: bool = False,
                     backup: Path | None = None, page_size: int = 500) -> dict:
    """Verify and recover an isolated snapshot, then optionally install once."""
    database = Path(database).resolve(strict=True)
    if not database.is_file():
        raise RecoveryFailure("DATABASE_NOT_FILE")
    if apply and backup is None:
        raise RecoveryFailure("BACKUP_REQUIRED")
    if not 1 <= page_size <= 10_000:
        raise RecoveryFailure("PAGE_SIZE_INVALID")
    with TemporaryDirectory(prefix="opentale-review-recovery-") as directory:
        copied = Path(directory) / "verified.sqlite3"
        if apply:
            backup = Path(backup).absolute()
            _backup(database, backup)
            _backup(backup, copied)
        else:
            _backup(database, copied)
        preview = GrowthStore(copied)
        try:
            state = preview.load_head(run_id)
            if state is None:
                raise RecoveryFailure("RUN_NOT_FOUND")
            before_head = digest(state.model_dump(mode="json"))
            before_usage = dict(state.config.get("usage", {}))
            before_counts = {kind: preview.count_objects(run_id, kind)
                             for kind in ("request", "trial", "candidate", "node", "response")}
            before_integrity = _verify(preview, run_id, page_size)
            recovery = recover_review_pauses(preview, state)
            after = preview.load_head(run_id)
            after_integrity = _verify(preview, run_id, page_size)
            counts_unchanged = all(preview.count_objects(run_id, kind) == count
                                   for kind, count in before_counts.items())
            usage_unchanged = after.config.get("usage", {}) == before_usage
            if not counts_unchanged or not usage_unchanged:
                raise RecoveryFailure("RECOVERY_CHANGED_FROZEN_WORK_OR_USAGE")
            if apply:
                live = GrowthStore(database)
                try:
                    with live.transaction():
                        current = live.load_head(run_id)
                        if (current is None or digest(current.model_dump(mode="json")) != before_head
                                or _last_object_sequence(live, run_id) != before_integrity["object_cursor"]):
                            raise RecoveryFailure("RUN_CHANGED_AFTER_BACKUP")
                        installed = recover_review_pauses(live, current)
                        if installed != recovery:
                            raise RecoveryFailure("RECOVERY_PLAN_CHANGED")
                finally:
                    live.close()
            return {"status": "applied" if apply else "dry_run", "run_id": run_id,
                    "recovered_count": recovery["recovered_count"],
                    "saved_draft_count": sum(item["saved_draft"] for item in recovery["recovered"]),
                    "deferred_reflect_count": sum(item["action"] == "reflect" for item in recovery["recovered"]),
                    "skipped_count": len(recovery["skipped"]), "usage_unchanged": usage_unchanged,
                    "frozen_work_counts_unchanged": counts_unchanged,
                    "before_verification": before_integrity, "after_verification": after_integrity,
                    "window_statuses": {key: actor.status for key, actor in after.windows.items()},
                    "provider_calls": 0, **({"backup": str(backup)} if apply else {})}
        finally:
            preview.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线恢复历史评议暂停；默认只在副本验收。")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只检查临时副本，默认启用。")
    mode.add_argument("--apply", action="store_true", help="验证副本后安装；需先停下该实验并指定新备份路径。")
    parser.add_argument("--backup", type=Path, help="--apply 必填；备份文件必须尚不存在。")
    parser.add_argument("--page-size", type=int, default=500)
    args = parser.parse_args(argv)
    if args.apply and args.backup is None:
        parser.error("--apply 必须提供 --backup，且备份路径不能已存在。")
    try:
        result = recover_database(args.database, args.run_id, apply=args.apply,
                                  backup=args.backup, page_size=args.page_size)
    except (RecoveryFailure, GrowthError) as exc:
        print(json.dumps({"status": "error", "code": exc.code, "provider_calls": 0}))
        return 1
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"status": "error", "code": "DATABASE_RECOVERY_FAILED", "provider_calls": 0}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
