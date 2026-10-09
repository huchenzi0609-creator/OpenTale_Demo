"""Incremental SQLite growth storage, separate from all legacy snapshot databases."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel

from .growth_types import GraphState, TextRef, canonical_scope, utc_now


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class GrowthStore:
    """A small mutable run head plus immutable indexed object versions and text blobs.

    Generic readers default to public visibility. Callers operating an actor scope
    must pass scope_id; diagnostic callers explicitly request include_private.
    """

    def __init__(self, root: Path | str):
        root = Path(root)
        if root.suffix in {".sqlite", ".sqlite3", ".db"}:
            self.path = root
            parent_existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            parent_existed = root.exists()
            root.mkdir(parents=True, exist_ok=True)
            self.path = root / "graph-runs.sqlite3"
        # Protect the new database itself; never chmod a pre-existing shared root.
        if not parent_existed:
            self.path.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, head_json TEXT NOT NULL,
                title TEXT NOT NULL, preset_id TEXT NOT NULL, status TEXT NOT NULL,
                graph_commit INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS texts (
                sha256 TEXT PRIMARY KEY, length INTEGER NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS objects (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id), kind TEXT NOT NULL,
                object_id TEXT NOT NULL, version TEXT NOT NULL, commit_seq INTEGER NOT NULL,
                private_scope TEXT, value_json TEXT NOT NULL,
                UNIQUE(run_id,kind,object_id,version)
            );
            CREATE INDEX IF NOT EXISTS objects_lookup
                ON objects(run_id,kind,object_id,commit_seq,seq);
            CREATE INDEX IF NOT EXISTS objects_scope_page
                ON objects(run_id,kind,private_scope,seq);
            CREATE TABLE IF NOT EXISTS object_heads (
                run_id TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL,
                object_seq INTEGER NOT NULL REFERENCES objects(seq),
                PRIMARY KEY(run_id,kind,object_id)
            );
            CREATE INDEX IF NOT EXISTS heads_page ON object_heads(run_id,kind,object_seq);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
                kind TEXT NOT NULL, payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
        """)
        self._db.commit()
        self._secure_files()

    def _secure_files(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[GrowthStore]:
        with self._lock:
            nested = self._db.in_transaction
            if not nested:
                self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self
                if not nested:
                    self._db.commit()
                    self._secure_files()
            except BaseException:
                if not nested:
                    self._db.rollback()
                raise

    def create_run(self, state: GraphState) -> GraphState:
        with self.transaction():
            if self.load_head(state.run_id) is not None:
                raise ValueError("Run already exists")
            self._db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (state.run_id, canonical(state.model_dump(mode="json")), state.title, state.preset_id,
                 state.status, state.graph_commit, state.created_at, state.updated_at),
            )
            head = state.model_dump(mode="json")
            self.record_event(state.run_id, "head_initial", {"head": head, "head_hash": digest(head)})
        return state

    def load_head(self, run_id: str) -> GraphState | None:
        with self._lock:
            row = self._db.execute("SELECT head_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return GraphState.model_validate_json(row[0]) if row else None

    def save_head(self, state: GraphState) -> GraphState:
        with self.transaction():
            previous = self.load_head(state.run_id)
            if previous is None:
                raise ValueError("Run does not exist")
            before, after = previous.model_dump(mode="json"), state.model_dump(mode="json")
            if before == after:
                return state
            result = self._db.execute(
                "UPDATE runs SET head_json=?,title=?,preset_id=?,status=?,graph_commit=?,updated_at=? WHERE run_id=?",
                (canonical(state.model_dump(mode="json")), state.title, state.preset_id,
                 state.status, state.graph_commit, state.updated_at, state.run_id),
            )
            if result.rowcount != 1:
                raise ValueError("Run does not exist")
            self.record_event(state.run_id, "head_transition", {
                "before_hash": digest(before), "head": after, "head_hash": digest(after),
            })
        return state

    def delete_run(self, run_id: str) -> bool:
        """Delete one root atomically; shared CAS bodies remain available to other roots.

        Orphan collection is deliberately separate: generic archived objects may
        contain nested references, so deleting a run never guesses body liveness.
        """
        with self.transaction():
            if self.load_head(run_id) is None:
                return False
            self._db.execute("DELETE FROM object_heads WHERE run_id=?", (run_id,))
            self._db.execute("DELETE FROM objects WHERE run_id=?", (run_id,))
            self._db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            self._db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        return True

    def fork_run(self, source_run_id: str, new_run_id: str | None = None, *, title: str | None = None) -> GraphState:
        """Create an explicit snapshot fork, retaining version identity and sharing CAS.

        The fork begins a new event lineage anchored in the source head. Earlier
        source actions are provenance, not falsely repeated fork executions.
        """
        with self.transaction():
            source = self.load_head(source_run_id)
            if source is None:
                raise ValueError("Source run does not exist")
            target = source.model_copy(deep=True)
            target.run_id = new_run_id or uuid4().hex
            if title is not None:
                target.title = title
            target.created_at = target.updated_at = utc_now()
            target.status = "idle"
            target.config["fork_source"] = {"run_id": source_run_id,
                                             "head_hash": digest(source.model_dump(mode="json"))}
            self.create_run(target)
            self._db.execute(
                "INSERT INTO objects(run_id,kind,object_id,version,commit_seq,private_scope,value_json) "
                "SELECT ?,kind,object_id,version,commit_seq,private_scope,value_json FROM objects "
                "WHERE run_id=? ORDER BY seq", (target.run_id, source_run_id),
            )
            self._db.execute(
                "INSERT INTO object_heads(run_id,kind,object_id,object_seq) "
                "SELECT ?,h.kind,h.object_id,t.seq FROM object_heads h "
                "JOIN objects s ON s.seq=h.object_seq JOIN objects t ON t.run_id=? "
                "AND t.kind=s.kind AND t.object_id=s.object_id AND t.version=s.version WHERE h.run_id=?",
                (target.run_id, target.run_id, source_run_id),
            )
            self.record_event(target.run_id, "fork", {"source_run_id": source_run_id,
                "source_head_hash": digest(source.model_dump(mode="json")),
                "source_graph_commit": source.graph_commit})
            return target

    def list_runs(self, *, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """An indexed summary query never loads run snapshots or object histories."""
        self._page_bounds(offset, limit)
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id,title,preset_id,status,graph_commit,created_at,updated_at FROM runs "
                "ORDER BY updated_at DESC,run_id LIMIT ? OFFSET ?", (limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def put_text(self, text: str) -> TextRef:
        ref = TextRef(sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), length=len(text))
        with self.transaction():
            self._db.execute("INSERT OR IGNORE INTO texts VALUES (?,?,?)", (ref.sha256, ref.length, text))
        return ref

    def read_text(self, ref: TextRef | dict | str, start: int = 0, end: int | None = None) -> str:
        sha = ref if isinstance(ref, str) else (ref.sha256 if isinstance(ref, TextRef) else ref["sha256"])
        with self._lock:
            row = self._db.execute("SELECT length,body FROM texts WHERE sha256=?", (sha,)).fetchone()
        if row is None:
            raise KeyError("Text body does not exist")
        length = row["length"]
        declared = ref.length if isinstance(ref, TextRef) else (ref.get("length") if isinstance(ref, dict) else None)
        if declared is not None and declared != length:
            raise ValueError("Text reference length mismatch")
        stop = length if end is None else end
        if start < 0 or stop < start or stop > length:
            raise ValueError("Text range is invalid")
        return row["body"][start:stop]

    def put_object(
        self, run_id: str, kind: str, value: BaseModel | dict, commit_seq: int | None = None,
    ) -> dict[str, Any]:
        item = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("An object needs a nonempty id")
        with self.transaction():
            head = self.load_head(run_id)
            if head is None:
                raise ValueError("Run does not exist")
            for field in ("scope_id", "private_scope"):
                if isinstance(item.get(field), str):
                    item[field] = canonical_scope(head, item[field])
            supplied = item.pop("version", None)
            version = digest(item)
            if supplied and supplied != version:
                raise ValueError("Immutable object version mismatch")
            item["version"] = version
            commit = head.graph_commit if commit_seq is None else commit_seq
            if commit < 0:
                raise ValueError("Commit cannot be negative")
            raw = canonical(item)
            self._db.execute(
                "INSERT OR IGNORE INTO objects(run_id,kind,object_id,version,commit_seq,private_scope,value_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, kind, item["id"], version, commit, item.get("private_scope"), raw),
            )
            row = self._db.execute(
                "SELECT seq,value_json FROM objects WHERE run_id=? AND kind=? AND object_id=? AND version=?",
                (run_id, kind, item["id"], version),
            ).fetchone()
            if row["value_json"] != raw:
                raise ValueError("Immutable object collision")
            self._db.execute(
                "INSERT INTO object_heads VALUES (?,?,?,?) ON CONFLICT(run_id,kind,object_id) "
                "DO UPDATE SET object_seq=MAX(object_seq,excluded.object_seq)",
                (run_id, kind, item["id"], row["seq"]),
            )
        return item

    @staticmethod
    def _visibility(scope_id: str | None, include_private: bool) -> tuple[str, list[Any]]:
        if include_private:
            return "", []
        if scope_id is None:
            return " AND o.private_scope IS NULL", []
        return " AND (o.private_scope IS NULL OR o.private_scope=?)", [scope_id]

    def get_object(
        self, run_id: str, kind: str, object_id: str, version: str | None = None,
        at_commit: int | None = None, scope_id: str | None = None, include_private: bool = False,
    ) -> dict[str, Any] | None:
        query = "SELECT o.value_json FROM objects o WHERE o.run_id=? AND o.kind=? AND o.object_id=?"
        args: list[Any] = [run_id, kind, object_id]
        if version is not None:
            query += " AND o.version=?"
            args.append(version)
        if at_commit is not None:
            query += " AND o.commit_seq<=?"
            args.append(at_commit)
        if scope_id is not None:
            head = self.load_head(run_id)
            if head is not None:
                scope_id = canonical_scope(head, scope_id)
        visible, values = self._visibility(scope_id, include_private)
        query += visible + " ORDER BY o.seq DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(query, args + values).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _page_bounds(offset: int, limit: int) -> None:
        if offset < 0 or limit < 1 or limit > 10_000:
            raise ValueError("Invalid page bounds")

    def list_objects(
        self, run_id: str, kind: str, at_commit: int | None = None,
        scope_id: str | None = None, include_private: bool = False,
        offset: int = 0, limit: int = 100,
        latest_first: bool = False,
    ) -> list[dict[str, Any]]:
        self._page_bounds(offset, limit)
        args: list[Any] = [run_id, kind]
        if at_commit is None:
            query = ("SELECT o.value_json FROM object_heads h JOIN objects o ON o.seq=h.object_seq "
                     "WHERE h.run_id=? AND h.kind=?")
        else:
            query = ("SELECT o.value_json FROM objects o WHERE o.run_id=? AND o.kind=? AND o.commit_seq<=? "
                     "AND o.seq=(SELECT MAX(v.seq) FROM objects v WHERE v.run_id=o.run_id "
                     "AND v.kind=o.kind AND v.object_id=o.object_id AND v.commit_seq<=?)")
            args.extend([at_commit, at_commit])
        if scope_id is not None:
            head = self.load_head(run_id)
            if head is not None:
                scope_id = canonical_scope(head, scope_id)
        visible, values = self._visibility(scope_id, include_private)
        query += visible + (" ORDER BY o.seq DESC" if latest_first else " ORDER BY o.seq") + " LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._db.execute(query, args + values + [limit, offset]).fetchall()
            return [json.loads(row[0]) for row in rows]

    def count_objects(self, run_id: str, kind: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM object_heads WHERE run_id=? AND kind=?", (run_id, kind),
            ).fetchone()[0]

    def list_versions(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Bounded archival pages, including superseded method and session versions."""
        self._page_bounds(0, limit)
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,kind,commit_seq,value_json FROM objects WHERE run_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (run_id, after_seq, limit),
            ).fetchall()
            return [{"seq": row["seq"], "kind": row["kind"], "commit_seq": row["commit_seq"],
                     "value": json.loads(row["value_json"])} for row in rows]

    def previous_event(self, run_id: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT seq,event_hash FROM events WHERE run_id=? AND seq<=? ORDER BY seq DESC LIMIT 1",
                (run_id, seq),
            ).fetchone()
            return dict(row) if row else None

    def record_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.transaction():
            previous = self._db.execute(
                "SELECT event_hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,),
            ).fetchone()
            previous_hash = previous[0] if previous else ""
            event_hash = digest({"kind": kind, "payload": payload, "previous_hash": previous_hash})
            result = self._db.execute(
                "INSERT INTO events(run_id,kind,payload_json,previous_hash,event_hash) VALUES (?,?,?,?,?)",
                (run_id, kind, canonical(payload), previous_hash, event_hash),
            )
            return {"seq": result.lastrowid, "kind": kind, "payload": payload,
                    "previous_hash": previous_hash, "event_hash": event_hash}

    def list_events(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._page_bounds(0, limit)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after_seq, limit),
            ).fetchall()
            return [{"seq": row["seq"], "kind": row["kind"], "payload": json.loads(row["payload_json"]),
                     "previous_hash": row["previous_hash"], "event_hash": row["event_hash"]} for row in rows]

    def iter_export(self, run_id: str, *, page_size: int = 100, include_bodies: bool = True) -> Iterator[dict[str, Any]]:
        """Yield NDJSON-ready evidence records from frozen index frontiers.

        The generator holds no SQLite transaction or thread-affine lock across
        yields. Concurrent appended objects cannot enter the captured prefix.
        The footer reports incomplete if the root was deleted during streaming.
        """
        self._page_bounds(0, page_size)
        with self.transaction():
            head = self.load(run_id)
            if head is None:
                raise ValueError("Run does not exist")
            objects = self._db.execute("SELECT MAX(seq),COUNT(*) FROM objects WHERE run_id=?", (run_id,)).fetchone()
            events = self._db.execute("SELECT MAX(seq),COUNT(*) FROM events WHERE run_id=?", (run_id,)).fetchone()
            object_frontier, object_count = objects[0] or 0, objects[1]
            event_frontier, event_count = events[0] or 0, events[1]
        value = head.model_dump(mode="json")
        yield {"record_type": "manifest", "format": "growth-reading-export-v1", "run_id": run_id,
               "head": value, "head_hash": digest(value), "object_frontier": object_frontier,
               "event_frontier": event_frontier, "object_versions": object_count, "events": event_count}
        seen_bodies: set[str] = set()

        def bodies(item: Any) -> Iterator[dict[str, Any]]:
            if isinstance(item, dict):
                if set(item) == {"sha256", "length"} and item["sha256"] not in seen_bodies:
                    seen_bodies.add(item["sha256"])
                    yield {"record_type": "body", "text_ref": item, "text": self.read_text(item)}
                else:
                    for child in item.values():
                        yield from bodies(child)
            elif isinstance(item, list):
                for child in item:
                    yield from bodies(child)

        if include_bodies:
            yield from bodies(value)
        exported_objects, exported_events = 0, 0
        cursor = 0
        while cursor < object_frontier:
            rows = self.list_versions(run_id, after_seq=cursor, limit=page_size)
            rows = [row for row in rows if row["seq"] <= object_frontier]
            if not rows:
                break
            for row in rows:
                yield {"record_type": "object_version", **row}
                if include_bodies:
                    yield from bodies(row["value"])
                exported_objects += 1
            cursor = rows[-1]["seq"]
        cursor = 0
        while cursor < event_frontier:
            rows = self.list_events(run_id, after_seq=cursor, limit=page_size)
            rows = [row for row in rows if row["seq"] <= event_frontier]
            if not rows:
                break
            for row in rows:
                yield {"record_type": "event", **row}
                if include_bodies:
                    yield from bodies(row["payload"])
                exported_events += 1
            cursor = rows[-1]["seq"]
        yield {"record_type": "end", "object_versions": exported_objects, "events": exported_events,
               "bodies": len(seen_bodies), "includes_bodies": include_bodies,
               "complete": exported_objects == object_count and exported_events == event_count}

    create = create_run
    load = load_head
    save = save_head
    put_body = put_text
    get_body = read_text
    put = put_object
    get = get_object
    list = list_objects
