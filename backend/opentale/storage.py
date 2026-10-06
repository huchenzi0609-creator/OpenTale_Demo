"""SQLite snapshots and append-only execution evidence, never fiction truth."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from .domain import Event, Limits, Snapshot, now


class StorageError(Exception):
    pass


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "opentale.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (run_id TEXT NOT NULL, id TEXT NOT NULL, "
                "event TEXT NOT NULL, PRIMARY KEY(run_id,id))"
            )
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def save(self, snapshot: Snapshot) -> None:
        snapshot.mark_extended_schema()
        snapshot.run.updated_at = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",
                (snapshot.run.id, snapshot.model_dump_json(exclude={"story", "reading"})),
            )
            db.executemany(
                "INSERT OR IGNORE INTO events VALUES (?,?,?)",
                [(snapshot.run.id, e.id, e.model_dump_json()) for e in snapshot.events],
            )

    def load(self, run_id: str) -> Snapshot:
        with self.connect() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        try:
            return Snapshot.model_validate_json(row[0])
        except Exception as exc:
            raise StorageError("存档版本或结构无法加载；原文件保持不动。") from exc

    def list(self) -> list[Snapshot]:
        with self.connect() as db:
            rows = db.execute("SELECT snapshot FROM runs ORDER BY rowid DESC").fetchall()
        return [Snapshot.model_validate_json(row[0]) for row in rows]

    def recover(self) -> None:
        for state in self.list():
            changed = False
            if state.run.usage.reserved_tokens:
                state.run.usage.unknown_tokens += state.run.usage.reserved_tokens
                state.run.usage.reserved_tokens = 0
                changed = True
            if state.run.status == "running":
                state.run.status = "paused"
                state.run.last_error = {
                    "code": "PROCESS_RESTARTED",
                    "message": "进程已重启，可从已保存阶段继续。",
                    "retryable": True,
                }
                changed = True
            if changed:
                self.save(state)

    def delete(self, run_id: str) -> None:
        self.load(run_id)
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def read_events(self, run_id: str) -> list[Event]:
        self.load(run_id)
        with self.connect() as db:
            rows = db.execute("SELECT event FROM events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    def fork(self, run_id: str, title: str | None = None, *, limits: Limits | None = None) -> Snapshot:
        state = self.load(run_id).model_copy(deep=True)
        state.run.fork_of = run_id
        state.run.id = uuid4().hex
        state.run.title = title or f"{state.run.title} · 分支"
        state.run.status = "idle"
        state.run.last_error = None
        state.run.created_at = now()
        # A fork retains pending semantic state and evidence; its API accounting starts anew.
        state.run.usage.calls = 0
        state.run.usage.input_tokens = 0
        state.run.usage.output_tokens = 0
        state.run.usage.reserved_tokens = 0
        state.run.usage.unknown_tokens = 0
        state.run.usage.failed_calls = 0
        state.run.usage.windows = 0
        if limits is not None:
            state.run.limits = limits.model_copy(deep=True)
        self.save(state)
        return state
