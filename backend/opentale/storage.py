"""SQLite snapshots and append-only execution evidence, never fiction truth."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
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
            db.execute("CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (run_id TEXT NOT NULL, id TEXT NOT NULL, "
                       "evidence TEXT NOT NULL, sealed INTEGER NOT NULL, PRIMARY KEY(run_id,id))")
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def save(self, snapshot: Snapshot) -> None:
        snapshot.mark_extended_schema()
        snapshot.run.updated_at = now()
        with self.connect() as db:
            def encode(value):
                if snapshot.schema_version != 5:
                    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                return json.dumps({"storage_format": "opentale-cas-v1", "root": self._pack(db, value)})

            if snapshot.schema_version == 5:
                # Historical events and accepted requests are immutable records. Pack only additions
                # and in-flight requests, not the complete accumulated graph on every checkpoint.
                event_ids = {row[0] for row in db.execute("SELECT id FROM events WHERE run_id=?", (snapshot.run.id,))}
                sealed = {row[0] for row in db.execute("SELECT id FROM requests WHERE run_id=? AND sealed=1",
                                                     (snapshot.run.id,))}
                for event in snapshot.events:
                    if event.id not in event_ids:
                        db.execute("INSERT INTO events VALUES (?,?,?)",
                                   (snapshot.run.id, event.id, encode(event.model_dump())))
                for request in snapshot.requests:
                    if request.id not in sealed:
                        db.execute("INSERT INTO requests VALUES (?,?,?,?) ON CONFLICT(run_id,id) DO UPDATE "
                                   "SET evidence=excluded.evidence,sealed=excluded.sealed",
                                   (snapshot.run.id, request.id, encode(request.model_dump()),
                                    int(request.accepted_result_hash is not None)))
                base = snapshot.model_dump(exclude={"story", "reading", "events", "requests"})
                serialized = json.dumps({
                    "storage_format": "opentale-checkpoint-v2", "root": self._pack(db, base),
                    "event_ids": [event.id for event in snapshot.events],
                    "request_ids": [request.id for request in snapshot.requests],
                })
            else:
                serialized = encode(snapshot.model_dump(exclude={"story", "reading"}))
                db.executemany(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?)",
                    [(snapshot.run.id, e.id, e.model_dump_json()) for e in snapshot.events],
                )
            db.execute(
                "INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",
                (snapshot.run.id, serialized),
            )
            if snapshot.schema_version == 5 and snapshot.pending is None:
                self._collect_objects(db)

    @staticmethod
    def _pack(db: sqlite3.Connection, value):
        """Tagged nodes prevent user text/dictionaries from impersonating storage references."""
        if isinstance(value, dict):
            node = {"kind": "dict", "items": {key: Store._pack(db, child) for key, child in value.items()}}
        elif isinstance(value, list):
            node = {"kind": "list", "items": [Store._pack(db, child) for child in value]}
        elif isinstance(value, str) and len(value) >= 512:
            node = {"kind": "text", "value": value}
        else:
            return value
        body = json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO objects VALUES (?,?)", (key, body))
        return {"$node": key}

    @staticmethod
    def _decode(db: sqlite3.Connection, raw: str):
        data = json.loads(raw)
        if data.get("storage_format") != "opentale-cas-v1":
            return data
        memo, visiting = {}, set()

        def unpack(value):
            if not isinstance(value, dict):
                return value
            if set(value) != {"$node"}:
                raise StorageError("存档对象引用无效。")
            key = value["$node"]
            if key in memo:
                # Deduplication is physical, never shared mutable domain identity.
                return deepcopy(memo[key])
            if key in visiting:
                raise StorageError("存档对象引用成环。")
            row = db.execute("SELECT body FROM objects WHERE id=?", (key,)).fetchone()
            if row is None or hashlib.sha256(row[0].encode()).hexdigest() != key:
                raise StorageError("存档对象缺失或校验失败。")
            visiting.add(key)
            node = json.loads(row[0])
            if node["kind"] == "dict":
                result = {name: unpack(child) for name, child in node["items"].items()}
            elif node["kind"] == "list":
                result = [unpack(child) for child in node["items"]]
            elif node["kind"] == "text":
                result = node["value"]
            else:
                raise StorageError("存档对象类型无效。")
            visiting.remove(key)
            memo[key] = result
            return result

        return unpack(data["root"])

    def load(self, run_id: str) -> Snapshot:
        with self.connect() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            try:
                manifest = json.loads(row[0])
                if manifest.get("storage_format") == "opentale-checkpoint-v2":
                    base = self._decode(db, json.dumps({"storage_format": "opentale-cas-v1",
                                                       "root": manifest["root"]}))
                    events = dict(db.execute("SELECT id,event FROM events WHERE run_id=?", (run_id,)))
                    requests = dict(db.execute("SELECT id,evidence FROM requests WHERE run_id=?", (run_id,)))
                    base["events"] = [self._decode(db, events[key]) for key in manifest["event_ids"]]
                    base["requests"] = [self._decode(db, requests[key]) for key in manifest["request_ids"]]
                else:
                    base = self._decode(db, row[0])
                return Snapshot.model_validate(base)
            except Exception as exc:
                raise StorageError("存档版本或结构无法加载；原文件保持不动。") from exc

    def list(self) -> list[Snapshot]:
        with self.connect() as db:
            rows = db.execute("SELECT id FROM runs ORDER BY rowid DESC").fetchall()
        return [self.load(row[0]) for row in rows]

    def recover(self) -> None:
        for state in self.list():
            changed = False
            for request in state.requests:
                for attempt in request.attempts:
                    if attempt.status == "started":
                        attempt.status = "interrupted"
                        attempt.error_code = "PROCESS_RESTARTED"
                        changed = True
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
            db.execute("DELETE FROM requests WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._collect_objects(db)

    @staticmethod
    def _collect_objects(db: sqlite3.Connection) -> None:
        """Retain nodes reachable from any saved branch or event, then collect stale checkpoints."""
        roots = db.execute("SELECT snapshot FROM runs UNION ALL SELECT event FROM events "
                           "UNION ALL SELECT evidence FROM requests").fetchall()
        pending = []
        for (raw,) in roots:
            value = json.loads(raw)
            if value.get("storage_format") in {"opentale-cas-v1", "opentale-checkpoint-v2"}:
                pending.append(value["root"]["$node"])
        seen = set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            row = db.execute("SELECT body FROM objects WHERE id=?", (key,)).fetchone()
            if row is None or hashlib.sha256(row[0].encode()).hexdigest() != key:
                raise StorageError("存档对象缺失，已停止清理。")
            node = json.loads(row[0])
            children = node.get("items", [])
            children = children.values() if isinstance(children, dict) else children
            pending.extend(child["$node"] for child in children if isinstance(child, dict))
        keys = [row[0] for row in db.execute("SELECT id FROM objects") if row[0] not in seen]
        db.executemany("DELETE FROM objects WHERE id=?", [(key,) for key in keys])

    def read_events(self, run_id: str) -> list[Event]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError(run_id)
            rows = db.execute("SELECT event FROM events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            return [Event.model_validate(self._decode(db, row[0])) for row in rows]

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
