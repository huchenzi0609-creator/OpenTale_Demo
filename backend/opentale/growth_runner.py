"""Independent, recoverable graph work sessions. Network calls never enter the kernel."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .domain import Limits
from .growth_kernel import GrowthError, add_node, add_passage, navigate
from .growth_methods import compile_method
from .growth_store import GrowthStore
from .growth_types import GraphState, MethodVersion, ObjectRef, ReadingTrack, SourceSpan, WindowState
from .kernel import KernelError
from .providers.base import ProviderError
from .security import contains_credential


def identifier(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


class GrowthRunner:
    def __init__(self, directory: Path, settings: Settings):
        self.store = GrowthStore(directory)
        self.settings = settings
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
        self.changes: dict[str, asyncio.Event] = {}
        self.notified_commits: dict[str, int] = {}

    def load(self, run_id):
        state = self.store.load_head(run_id)
        if state is None:
            raise KernelError("RUN_NOT_FOUND", "该生长实验不存在。")
        return state

    def put(self, run_id, kind, value, **kwargs):
        data = dict(value)
        data.pop("version", None)
        return self.store.put(run_id, kind, data, **kwargs)

    def provider(self, state):
        from .providers.growth import make_growth_provider
        return make_growth_provider(state.config.get("provider", "fixture"), self.settings)

    def ensure_safe(self, value):
        if contains_credential(value, self.settings.api_key.get_secret_value()):
            raise KernelError("CONTENT_UNSAFE", "内容中不能包含当前使用的密钥。")
        return value

    def create(self, *, title, provider, windows, shared_seed="", limits=None, preset_id="custom"):
        self.ensure_safe({"title": title, "provider": provider, "windows": windows,
                          "shared_seed": shared_seed, "preset_id": preset_id})
        if provider not in {"fixture", "deepseek"}:
            raise KernelError("PROVIDER_INVALID", "请选择有效的生成方式。")
        if not windows or len({w["id"] for w in windows}) != len(windows):
            raise KernelError("WINDOW_INVALID", "窗口 ID 须唯一，至少设置一个窗口。")
        state = GraphState(run_id=uuid4().hex, title=title, preset_id=preset_id,
                           config={"provider": provider, "limits": (limits or Limits(
                               max_calls=None, max_tokens=None, max_windows=None)).model_dump(),
                                   "usage": {"calls": 0, "failed_calls": 0, "input_tokens": 0,
                                             "output_tokens": 0, "unknown_tokens": 0, "reserved_tokens": 0}})
        for w in windows:
            if contains_credential(w.get("seed", ""), self.settings.api_key.get_secret_value()) or contains_credential(w.get("direction", ""), self.settings.api_key.get_secret_value()):
                raise KernelError("CONTENT_UNSAFE", "角色材料中不能包含密钥。")
            state.windows[w["id"]] = WindowState(id=w["id"], label=w.get("label", w["id"]),
                                                  direction=w.get("direction", ""),
                                                  seed_text_ref=self.store.put_text(w.get("seed", "")))
        self.store.create_run(state)
        for w in windows:
            state = add_node(self.store, state, w["id"], w.get("seed", "") or w.get("label", w["id"]),
                             request_id=f"seed:{w['id']}")
            window = state.windows[w["id"]]
            window.entry_node_ids = [window.working_node_id]
        if shared_seed:
            if contains_credential(shared_seed, self.settings.api_key.get_secret_value()):
                raise KernelError("CONTENT_UNSAFE", "共同材料中不能包含密钥。")
            first = windows[0]["id"]
            state = add_node(self.store, state, first, shared_seed, request_id="shared-seed")
            shared = state.windows[first].working_node_id
            for window in state.windows.values():
                window.entry_node_ids.append(shared)
                window.frontier_node_ids = list(dict.fromkeys(window.entry_node_ids))
        for window in state.windows.values():
            scope = f"window:{window.id}"
            method = MethodVersion(id=f"method-{identifier(scope)}", scope_id=scope,
                                   instruction=("从已构造的生活中发现值得继续的方向；用具体经验展开关系，"
                                                "让行动留下可继续承接的后果。"),
                                   program={"node_ids": window.entry_node_ids}, private_scope=scope,
                                   created_commit=state.graph_commit)
            row = self.put(state.run_id, "method", method.model_dump(), commit_seq=state.graph_commit)
            window.method_ref = ObjectRef(kind="method", object_id=row["id"], version=row["version"])
        state.status = "idle"
        self.store.save_head(state)
        self.event(state, "created", {"mode": "growth_reading"})
        return self.snapshot(state.run_id)

    def event(self, state, action, details):
        self.put(state.run_id, "event", {"id": uuid4().hex, "action": action,
                       "graph_commit": state.graph_commit, "details": details}, commit_seq=state.graph_commit)
        if state.graph_commit > self.notified_commits.get(state.run_id, -1):
            self.notified_commits[state.run_id] = state.graph_commit
            self.changes.setdefault(state.run_id, asyncio.Event()).set()

    def rows(self, state, kind, scope=None, *, offset=0, limit=100, owner=False, horizon=None, latest=False):
        return self.store.list(state.run_id, kind, offset=offset, limit=limit, scope_id=scope,
                               include_private=owner, at_commit=horizon, latest_first=latest)

    def body(self, row):
        ref = row.get("text_ref")
        return self.store.read_text(ref) if ref else row.get("text", "")

    def expand(self, state, kind, row):
        result = dict(row)
        if kind == "trial" and row.get("result_ref"):
            ref = row["result_ref"]
            candidate = self.store.get(state.run_id, ref["kind"], ref["object_id"],
                                       version=ref["version"], include_private=True)
            if candidate:
                result["text"] = self.body(candidate)
                result["parent_ids"] = candidate.get("parent_ids", [])
        elif row.get("text_ref"):
            result["text"] = self.body(row)
        return result

    def snapshot(self, run_id):
        state = self.load(run_id)
        active = [scope for (rid, scope), task in self.tasks.items() if rid == run_id and not task.done()]
        usage = {**state.config["usage"], "windows": state.graph_commit}
        tracks = []
        for track in state.reading_tracks.values():
            row = track.model_dump()
            row["title"] = track.label
            row["status"] = "running" if f"reading:{track.id}" in active else track.status
            row["visits"] = [v for v in self.rows(state, "visit", f"reading:{track.id}", latest=True)[::-1]
                             if v["track_id"] == track.id]
            row["passages"] = [self.expand(state, "passage", p) for p in
                               self.rows(state, "passage", f"reading:{track.id}", latest=True)[::-1]
                               if p["track_id"] == track.id]
            tracks.append(row)
        return self.ensure_safe({"run": {"id": run_id, "title": state.title, "provider": state.config["provider"],
                        "mode": "growth_reading", "revision": state.graph_commit,
                        "status": "running" if active else state.status, "limits": state.config["limits"],
                        "usage": usage}, "graph_commit": state.graph_commit, "counts": state.counts,
                "schema_version": 6, "engine_version": state.engine_version,
                "windows": [w.model_dump() for w in state.windows.values()],
                "nodes": self.rows(state, "node", owner=True), "links": self.rows(state, "link", owner=True),
                "readings": tracks, "active_scopes": active,
                **{kind + "s": [self.expand(state, kind, row) for row in
                                 self.rows(state, kind, owner=True, latest=True)[::-1]]
                   for kind in ("method", "trial", "response", "receipt", "event", "session")}})

    def add_reading(self, run_id, title, entry_node_ids=None, direction=""):
        self.ensure_safe({"title": title, "entry_node_ids": entry_node_ids, "direction": direction})
        state = self.load(run_id)
        entries = entry_node_ids or [next(iter(state.windows.values())).entry_node_ids[0]]
        for node_id in entries:
            if not self.store.get(run_id, "node", node_id, include_private=False):
                raise KernelError("TARGET_UNKNOWN", "阅读入口不存在或不可访问。")
        track_id = uuid4().hex[:16]
        scope = f"reading:{track_id}"
        method = MethodVersion(id=f"method-{identifier(scope)}", scope_id=scope,
                               instruction="沿已有叙事选择值得进入的枝桠，保留不同位置的差异；根据访问形成阅读。",
                               program={"node_ids": entries}, private_scope=scope,
                               created_commit=state.graph_commit)
        row = self.put(run_id, "method", method.model_dump(), commit_seq=state.graph_commit)
        state.reading_tracks[track_id] = ReadingTrack(id=track_id, label=title, entry_node_ids=entries,
            observed_graph_commit=state.graph_commit, direction=direction,
            method_ref=ObjectRef(kind="method", object_id=row["id"], version=row["version"]))
        self.store.save_head(state)
        self.event(state, "reading_created", {"track_id": track_id})
        return self.snapshot(run_id)

    def session(self, state, scope):
        key = f"session-{identifier(scope)}"
        return self.store.get(state.run_id, "session", key, include_private=True) or {
            "id": key, "scope_id": scope, "private_scope": scope, "work_seq": 0,
            "last_action": "ready", "status": "ready", "pages": [], "selected_node_ids": []}

    def actor(self, state, scope):
        kind, key = scope.split(":", 1)
        mapping = state.windows if kind == "window" else state.reading_tracks
        if key not in mapping:
            raise KernelError("SCOPE_INVALID", "工作位置不存在。")
        return mapping[key]

    def frame(self, state, scope, session):
        actor = self.actor(state, scope)
        nodes = self.rows(state, "node", scope, horizon=state.graph_commit, limit=40, latest=True)[::-1]
        methods = self.rows(state, "method", scope, limit=20, latest=True)[::-1]
        links = self.rows(state, "link", scope, horizon=state.graph_commit, limit=40, latest=True)[::-1]
        receipts = [r for r in self.rows(state, "receipt", scope, limit=12, latest=True)[::-1]
                    if r.get("scope_id") == scope]
        feedback = [r for r in self.rows(state, "response", scope, limit=20, latest=True)[::-1]
                    if r.get("scope_id") == scope]
        unseen = [r for r in feedback if r["id"] not in session.get("seen_feedback_ids", [])]
        chosen_feedback = unseen[:1] or feedback[-1:]
        responses = [self.expand(state, "response", r) for r in chosen_feedback]
        method_ref = session.get("applied_method") or (actor.method_ref.model_dump() if actor.method_ref else None)
        method = (self.store.get(state.run_id, "method", method_ref["object_id"],
                                version=method_ref["version"], scope_id=scope) if method_ref else None)
        selected = list(session.get("selected_node_ids") or actor.entry_node_ids)
        current = getattr(actor, "working_node_id", None) or getattr(actor, "cursor_node_id", None)
        if current and current not in selected:
            selected.append(current)
        sources = []
        source_page_chars = max(64, min(2500, self.settings.context_target_chars // max(1, len(selected) * 8)))
        for key in selected:
            node = self.store.get(state.run_id, "node", key, scope_id=scope)
            if node:
                text = self.body(node)
                # A page, never a silently complete source. The full body remains readable.
                end = min(len(text), source_page_chars)
                if key not in {n["id"] for n in nodes}:
                    nodes.append(node)
                sources.append({"id": key, "version": node["version"], "text": text[:end],
                                "start": 0, "end": end, "full_length": len(text), "partial": end < len(text)})
        pending = None
        if session.get("pending_trial_id"):
            trial = self.store.get(state.run_id, "trial", session["pending_trial_id"], scope_id=scope)
            if trial:
                pending = self.expand(state, "trial", trial)
        visits = [v for v in self.rows(state, "visit", scope, latest=True)[::-1]
                  if v.get("track_id") == scope.split(":", 1)[1]]
        reading_history = []
        if scope.startswith("reading:"):
            for passage in self.rows(state, "passage", scope, latest=True, limit=3)[::-1]:
                text = self.body(passage)
                reading_history.append({"id": passage["id"], "version": passage["version"],
                    "text": text[:2000], "start": 0, "end": min(2000, len(text)), "full_length": len(text),
                    "source_spans": passage["source_spans"]})
        return {"scope_id": scope, "role": "window" if scope.startswith("window:") else "reading",
                "graph_commit": state.graph_commit, "work_seq": session["work_seq"],
                "actor_revision": actor.revision,
                "direction": actor.direction, "cursor": getattr(actor, "cursor_node_id", None)
                    or getattr(actor, "working_node_id", None),
                "frontier": getattr(actor, "frontier_node_ids", []) or actor.entry_node_ids,
                "entry_node_ids": actor.entry_node_ids,
                "allowed_actions": ["query", "read_page", "inspect", "apply", "revise", "adopt", "respond", "wait",
                                    "try_write", "publish"]
                    + ([] if scope.startswith("window:") else ["navigate", "render"]),
                "nodes": [{"id": n["id"], "version": n["version"], "created_commit": n["created_commit"],
                           "text_preview": self.body(n)[:120] if not n.get("private_scope") else "",
                           "text_ref": n["text_ref"]} for n in nodes],
                "methods": [{**m, "instruction": m["instruction"][:160]} for m in methods],
                "links": links, "method": method, "sources": sources, "view": session.get("view"),
                "pending_trial": pending, "trials": self.rows(state, "trial", scope), "visits": visits,
                "responses": responses, "receipts": receipts[-12:], "pages": session.get("pages", []),
                "reading_history": reading_history,
                "feedback_catalog": [{"id": r["id"], "version": r["version"], "length": r["text_ref"]["length"]}
                                     for r in feedback],
                "catalog_page": {"offset": 0, "limit": 100, "more_via": "query"}}

    async def call(self, run_id, scope, sequence, stage, frame, *, repair=None, repair_attempt=0):
        state = self.load(run_id)
        parallel = state.config["limits"].get("max_parallel")
        if parallel is None:
            return await self._call(run_id, scope, sequence, stage, frame, repair=repair, repair_attempt=repair_attempt)
        semaphore = self.semaphores.setdefault((run_id, parallel), asyncio.Semaphore(parallel))
        async with semaphore:
            return await self._call(run_id, scope, sequence, stage, frame, repair=repair, repair_attempt=repair_attempt)

    async def _call(self, run_id, scope, sequence, stage, frame, *, repair=None, repair_attempt=0):
        state = self.load(run_id)
        provider = self.provider(state)
        request_id = f"request-{identifier(scope, sequence, stage)}"
        existing = self.store.get(run_id, "request", request_id, include_private=True)
        if existing and existing.get("status") == "accepted":
            return self.ensure_safe(existing["result"]), request_id
        if existing and existing.get("status") == "calling":
            usage = state.config["usage"]
            prior = existing.get("reserved_tokens")
            if prior is None:
                prior = max(1, len(json.dumps(existing["projection"], ensure_ascii=False)) // 2)
                prior += existing["projection"].get("max_tokens", self.settings.max_output_tokens)
            unsettled = min(prior, usage.get("reserved_tokens", 0))
            usage["reserved_tokens"] -= unsettled
            usage["unknown_tokens"] += unsettled
            usage["failed_calls"] += 1
            existing["status"] = "interrupted"
            existing.setdefault("attempts", []).append({"status": "interrupted", "reserved_tokens": unsettled})
            with self.store.transaction():
                self.put(run_id, "request", existing)
                self.store.save_head(state)
        if existing:
            frame = existing["frame"]
        projection = provider.projection(frame, stage, repair=repair)
        envelope = {"id": request_id, "scope_id": scope, "private_scope": scope, "stage": stage,
                    "frame": frame, "projection": projection, "status": "calling",
                    "attempts": list(existing.get("attempts", [])) if existing else [],
                    "graph_commit": state.graph_commit}
        limits, usage = state.config["limits"], state.config["usage"]
        estimate = max(1, len(json.dumps(projection, ensure_ascii=False)) // 2) + self.settings.max_output_tokens
        total = sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "unknown_tokens", "reserved_tokens"))
        if ((limits.get("max_calls") is not None and usage["calls"] >= limits["max_calls"])
                or (limits.get("max_tokens") is not None and total + estimate > limits["max_tokens"])):
            raise KernelError("BUDGET_EXHAUSTED", "预算已用完，工作位置已保留。")
        usage["calls"] += 1
        usage["reserved_tokens"] += estimate
        envelope["reserved_tokens"] = estimate
        actor = self.actor(state, scope)
        actor.status = "calling"
        calling_session = self.session(state, scope)
        calling_session["status"] = "calling"
        with self.store.transaction():
            self.put(run_id, "request", envelope)
            self.put(run_id, "session", calling_session)
            self.store.save_head(state)
        try:
            result = await getattr(provider, stage)(frame, repair=repair)
            self.ensure_safe(result.value)
        except BaseException as exc:
            state = self.load(run_id)
            usage = state.config["usage"]
            usage["reserved_tokens"] -= estimate
            known = exc.usage if isinstance(exc, ProviderError) else None
            if known:
                usage["input_tokens"] += known[0]
                usage["output_tokens"] += known[1]
            else:
                usage["unknown_tokens"] += estimate
            usage["failed_calls"] += 1
            envelope.update(status="failed", error_code=getattr(exc, "code", "REQUEST_INTERRUPTED"))
            envelope["attempts"].append({"status": "failed", "projection": projection,
                                         "error_code": envelope["error_code"], "usage": known})
            self.put(run_id, "request", envelope)
            self.store.save_head(state)
            if isinstance(exc, ProviderError) and exc.feedback is not None and repair_attempt < 2:
                return await self._call(run_id, scope, sequence, stage, frame,
                                       repair=exc.feedback, repair_attempt=repair_attempt + 1)
            raise
        state = self.load(run_id)
        usage = state.config["usage"]
        usage["reserved_tokens"] -= estimate
        usage["input_tokens"] += result.input_tokens
        usage["output_tokens"] += result.output_tokens
        envelope.update(status="accepted", result=result.value,
                        input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        envelope["attempts"].append({"status": "accepted", "projection": projection,
                                     "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        with self.store.transaction():
            self.put(run_id, "request", envelope)
            self.store.save_head(state)
        return result.value, request_id

    async def step_scope(self, run_id, scope):
        lock = self.locks.setdefault((run_id, scope), asyncio.Lock())
        if lock.locked():
            raise KernelError("RUN_BUSY", "该工作位置正在处理一个动作。")
        async with lock:
            state = self.load(run_id)
            session = self.session(state, scope)
            seq = session["work_seq"] + 1
            frame = self.frame(state, scope, session)
            try:
                decision, request_id = await self.call(run_id, scope, seq, "decide", frame)
                state = self.load(run_id)
                frame = self.store.get(run_id, "request", request_id, include_private=True)["frame"]
                # Retain accepted decisions until their deterministic installation succeeds.
                result = await self.execute(state, scope, session, decision, frame, request_id, seq)
                state = self.load(run_id)
                session.update(work_seq=seq, last_action=decision["action"], status="waiting" if decision["action"] == "wait"
                               else "ready", error=None)
                if decision["action"] == "wait":
                    session["waiting_commit"] = frame["graph_commit"]
                session["seen_feedback_ids"] = list(dict.fromkeys([
                    *session.get("seen_feedback_ids", []), *[r["id"] for r in frame.get("responses", [])]]))[-40:]
                receipt = {"id": f"receipt-{identifier(scope, seq)}", "session_id": session["id"],
                    "scope_id": scope, "private_scope": scope, "action": decision["action"],
                    "args": decision.get("args", {}), "details": result, "graph_commit": state.graph_commit}
                self.put(run_id, "receipt", receipt, commit_seq=state.graph_commit)
                self.put(run_id, "session", session)
                self.actor(state, scope).status = session["status"]
                state.status = "idle"
                self.store.save_head(state)
                self.event(state, decision["action"], {"scope_id": scope, "work_seq": seq, **result})
            except (KernelError, GrowthError, ProviderError) as exc:
                state = self.load(run_id)
                session.update(status="error", error={"code": exc.code, "message": exc.message})
                self.put(run_id, "session", session)
                self.actor(state, scope).status = "error"
                state.status = "error"
                self.store.save_head(state)
                raise
            return self.snapshot(run_id)

    async def execute(self, state, scope, session, decision, frame, request_id, seq):
        action, args = decision["action"], decision.get("args", {})
        self.ensure_safe(decision)
        actor = self.actor(state, scope)
        rid = state.run_id
        offered_versions = {}
        for catalog, kind in (("methods", "method"), ("nodes", "node"), ("trials", "trial"),
                              ("visits", "visit"), ("responses", "response"), ("links", "link")):
            offered_versions.update({(kind, row["id"]): row["version"] for row in frame.get(catalog, [])
                                     if row.get("version")})
        if frame.get("method"):
            offered_versions[("method", frame["method"]["id"])] = frame["method"]["version"]
        def get(kind, key, version=None):
            row = self.store.get(rid, kind, key, scope_id=scope,
                                 version=version or offered_versions.get((kind, key)))
            if row is None:
                raise KernelError("TARGET_UNKNOWN", "对象不存在或当前工作位置不可访问。")
            return row
        if action == "query":
            kind = args.get("kind", "node")
            rows = self.rows(state, kind, scope, offset=args.get("offset", 0))
            needle = args.get("query", "").casefold()
            rows = [r for r in rows if not needle or needle == "*"
                    or any(term in (self.body(r) + r.get("instruction", "")).casefold() for term in needle.split())]
            session["pages"] = [{"kind": kind, "items": [{"id": r["id"], "version": r["version"],
                                  **({k: r[k] for k in ("source_id", "target_id", "kind", "relation")}
                                     if kind == "link" else {}),
                                  "text_preview": (self.body(r) or r.get("instruction", ""))[:160]} for r in rows],
                                 "offset": args.get("offset", 0), "next_offset": args.get("offset", 0) + 100}]
            return {"objects": [r["id"] for r in rows], "page": session["pages"][0]}
        if action == "read_page":
            row = get(args.get("kind", "node"), args["id"])
            expanded = self.expand(state, args.get("kind", "node"), row)
            text = expanded.get("text", row.get("instruction", ""))
            if args.get("kind") in {"link", "view"}:
                text = json.dumps(row, ensure_ascii=False)
            start, end = args.get("start", 0), args.get("end", min(len(text), args.get("start", 0) + 2500))
            if not 0 <= start <= end <= len(text):
                raise KernelError("SOURCE_RANGE_INVALID", "读取区间超出原文。")
            session["pages"] = [{"kind": args.get("kind", "node"), "id": row["id"],
                                 "version": row["version"], "text": text[start:end],
                                 "start": start, "end": end, "full_length": len(text),
                                 "next_start": end if end < len(text) else None}]
            if args.get("kind") in {"link", "view"}:
                session["pages"][0]["object"] = row if len(text) <= 2500 else {"id": row["id"],
                    "version": row["version"], "kind": args["kind"], "partial": True}
            return {"read": session["pages"][0]}
        if action in {"inspect", "apply", "adopt", "revise"}:
            method = get("method", args["method_id"], args.get("method_version"))
            ref = {"kind": "method", "object_id": method["id"], "version": method["version"]}
            if action == "inspect":
                session["pages"] = [{"method": method}]
            elif action == "apply":
                session["applied_method"] = ref
                view = compile_method(self.store, state, scope, ref, actor.entry_node_ids)
                session["selected_node_ids"] = [m.object_id for m in view.members]
                session["view"] = view.model_dump()
            elif action == "adopt":
                actor.method_ref = ObjectRef.model_validate(ref)
                session["applied_method"] = ref
                self.store.save_head(state)
            else:
                inputs = args.get("input_ids", [])
                evidence = []
                nodes = []
                for key in inputs:
                    found = None
                    for kind in ("node", "method", "response", "trial", "visit"):
                        found = self.store.get(rid, kind, key, scope_id=scope)
                        if found:
                            evidence.append(ObjectRef(kind=kind, object_id=key, version=found["version"]))
                            if kind == "node":
                                nodes.append(key)
                            break
                    if found is None:
                        raise KernelError("TARGET_UNKNOWN", "方法依据在此位置不可见。")
                new = MethodVersion(id=f"method-{identifier(scope, seq)}", scope_id=scope,
                    instruction=args["instruction"], program={**(args.get("program") or method.get("program") or {}),
                    "node_ids": (args.get("program") or {}).get("node_ids") if
                    "node_ids" in (args.get("program") or {}) else
                    nodes or (method.get("program") or {}).get("node_ids", actor.entry_node_ids),
                    "purpose": args.get("purpose", "")},
                    parent_ref=ObjectRef.model_validate(ref), source_refs=[ObjectRef.model_validate(ref), *evidence],
                    private_scope=scope, created_commit=state.graph_commit)
                row = self.put(rid, "method", new.model_dump(), commit_seq=state.graph_commit)
                return {"method_id": row["id"], "version": row["version"], "revises": ref}
            return {"method_id": method["id"], "version": method["version"],
                    "selected_node_ids": session.get("selected_node_ids", [])}
        if action == "try_write":
            frozen_method = frame.get("method")
            ref = ({"kind": "method", "object_id": frozen_method["id"], "version": frozen_method["version"]}
                   if frozen_method else None)
            if not ref:
                raise KernelError("METHOD_REQUIRED", "请先选择或试用一个工作方法。")
            parents = args["parent_ids"] if "parent_ids" in args else [getattr(actor, "working_node_id", None)
                                                 or getattr(actor, "cursor_node_id", None) or actor.entry_node_ids[0]]
            for key in parents:
                get("node", key)
            draft_frame = {**frame, "direction": args.get("direction") or actor.direction,
                           "parent_ids": parents, "method": get("method", ref["object_id"], ref["version"])}
            text, draft_request = await self.call(rid, scope, seq, "draft", draft_frame)
            frozen_draft = self.store.get(rid, "request", draft_request, include_private=True)["frame"]
            actual_method = frozen_draft["method"]
            ref = {"kind": "method", "object_id": actual_method["id"], "version": actual_method["version"]}
            used_spans = []
            for source in frozen_draft["sources"]:
                node = get("node", source["id"], source["version"])
                used_spans.append(SourceSpan(node_id=node["id"], node_version=node["version"],
                    text_ref=node["text_ref"], start=source.get("start", 0),
                    end=source.get("end", len(source["text"]))).model_dump())
            for node in frozen_draft.get("nodes", []):
                if node.get("text_preview"):
                    used_spans.append(SourceSpan(node_id=node["id"], node_version=node["version"],
                        text_ref=node["text_ref"], start=0, end=len(node["text_preview"])).model_dump())
            for page in frozen_draft.get("pages", []):
                if page.get("kind") == "node" and page.get("text"):
                    node = get("node", page["id"], page["version"])
                    used_spans.append(SourceSpan(node_id=node["id"], node_version=node["version"],
                        text_ref=node["text_ref"], start=page["start"], end=page["end"]).model_dump())
            candidate = self.put(rid, "candidate", {"id": f"candidate-{identifier(scope, seq)}",
                "scope_id": scope, "private_scope": scope, "text_ref": self.store.put_text(text).model_dump(),
                "parent_ids": parents, "method_ref": ref, "request_id": draft_request, "source_spans": used_spans})
            trial = self.put(rid, "trial", {"id": f"trial-{identifier(scope, seq)}", "scope_id": scope,
                "private_scope": scope, "method_ref": ref, "source_refs": [
                    {"kind": "node", "object_id": s["id"], "version": s["version"]}
                    for s in frozen_draft["sources"]],
                "result_ref": {"kind": "candidate", "object_id": candidate["id"], "version": candidate["version"]},
                "feedback_refs": [], "adopted": False})
            session["pending_trial_id"] = trial["id"]
            if args.get("direction"):
                current = self.load(rid)
                current_actor = self.actor(current, scope)
                if current_actor.revision == frame.get("actor_revision", current_actor.revision):
                    current_actor.direction = args["direction"]
                    current_actor.revision += 1
                    self.store.save_head(current)
            return {"trial_id": trial["id"], "method_ref": ref, "draft_request": draft_request}
        if action == "publish":
            trial = get("trial", args["trial_id"])
            candidate = get("candidate", trial["result_ref"]["object_id"], trial["result_ref"]["version"])
            state = self.load(rid)
            bound = state.config["limits"].get("max_windows")
            already = self.store.get(rid, "patch", request_id, include_private=True)
            if not already and bound is not None and state.config.get("growth_commits", 0) >= bound:
                raise KernelError("BUDGET_EXHAUSTED", "生长提交预算已用完，试写保留在工作区。")
            actor_id = scope.split(":", 1)[1]
            state = add_node(self.store, state, actor_id, self.body(candidate),
                             parents=args["parent_ids"] if "parent_ids" in args else candidate["parent_ids"],
                             associations=args.get("association_ids", []), request_id=request_id,
                             relation=args.get("relation", "继续生长"), author_scope=scope,
                             source_spans=candidate.get("source_spans", []),
                             input_refs=[ObjectRef.model_validate(r) for r in trial["source_refs"]
                                         + [trial["method_ref"]]])
            trial["adopted"] = True
            self.put(rid, "trial", trial)
            session.pop("pending_trial_id", None)
            if not already:
                state.config["growth_commits"] = state.config.get("growth_commits", 0) + 1
                self.store.save_head(state)
            node_id = (state.windows[actor_id].working_node_id if scope.startswith("window:") else
                       state.reading_tracks[actor_id].bookmark_node_ids[-1])
            return {"node_id": node_id,
                    "trial_id": trial["id"]}
        if action == "navigate":
            node = get("node", args["node_id"])
            start = args.get("start", 0)
            end = args.get("end", min(node["text_ref"]["length"], start + 2500))
            state = navigate(self.store, state, scope.split(":", 1)[1], args["node_id"],
                             via_link_id=args.get("edge_id"), transition=args.get("transition", "entry"),
                             request_id=request_id, observed_graph_commit=frame["graph_commit"], start=start, end=end)
            return {"cursor": state.reading_tracks[scope.split(":", 1)[1]].cursor_node_id,
                    "read_seq": state.reading_tracks[scope.split(":", 1)[1]].read_seq}
        if action == "render":
            visits = [get("visit", key) for key in args["visit_ids"]]
            track_id = scope.split(":", 1)[1]
            if not visits or any(v["track_id"] != track_id for v in visits):
                raise KernelError("READING_SOURCE_INVALID", "呈现只能使用本读轨已访问的内容。")
            sources, spans = [], []
            for visit in visits:
                node = get("node", visit["node_id"], visit["node_version"])
                for span in visit["source_spans"]:
                    text = self.store.read_text(span["text_ref"], span["start"], span["end"])
                    sources.append({"id": node["id"], "version": node["version"], "text": text,
                                    "start": span["start"], "end": span["end"]})
                    spans.append(span)
            render_frame = {key: frame[key] for key in ("scope_id", "role", "graph_commit", "work_seq",
                            "direction", "allowed_actions", "method", "reading_history", "responses") if key in frame}
            render_frame.update(sources=sources, visit_ids=args["visit_ids"], source_spans=spans)
            text, _ = await self.call(rid, scope, seq, "render", render_frame)
            state = self.load(rid)
            add_passage(self.store, state, track_id, text, visit_ids=args["visit_ids"],
                        source_spans=spans, request_id=request_id)
            return {"visit_ids": args["visit_ids"], "source_spans": spans}
        if action == "respond":
            frozen_method = frame.get("method")
            response = self.put(rid, "response", {"id": f"response-{identifier(scope, seq)}",
                "scope_id": scope, "private_scope": scope, "text_ref": self.store.put_text(args["text"]).model_dump(),
                "about_ids": args.get("about_ids", []), "method_ref": {"kind": "method",
                    "object_id": frozen_method["id"], "version": frozen_method["version"]} if frozen_method else None})
            if args.get("evaluation_method_id"):
                criterion = get("method", args["evaluation_method_id"])
                response["evaluation_method_ref"] = {"kind": "method", "object_id": criterion["id"],
                                                      "version": criterion["version"]}
                response = self.put(rid, "response", response)
            for key in args.get("about_ids", []):
                trial = self.store.get(rid, "trial", key, scope_id=scope)
                if trial:
                    feedback_ref = {"kind": "response", "object_id": response["id"], "version": response["version"]}
                    if feedback_ref not in trial.setdefault("feedback_refs", []):
                        trial["feedback_refs"].append(feedback_ref)
                    self.put(rid, "trial", trial)
            return {"response_id": response["id"]}
        if action == "wait":
            return {"reason": args.get("reason", "暂留当前方向。")}
        raise KernelError("ACTION_INVALID", "该工作动作未开放。")

    async def step(self, run_id, reading_id=None):
        state = self.load(run_id)
        if reading_id is not None:
            return await self.step_scope(run_id, f"reading:{reading_id}")
        windows = list(state.windows)
        index = state.config.get("next_window", 0) % len(windows)
        state.config["next_window"] = index + 1
        self.store.save_head(state)
        return await self.step_scope(run_id, f"window:{windows[index]}")

    async def start(self, run_id, reading_id=None):
        scope = f"reading:{reading_id}" if reading_id else "growth"
        key = (run_id, scope)
        if key not in self.tasks or self.tasks[key].done():
            state = self.load(run_id)
            scopes = [scope] if reading_id else [f"window:{window_id}" for window_id in state.windows]
            if any(self.locks.get((run_id, item)) and self.locks[(run_id, item)].locked() for item in scopes):
                raise KernelError("RUN_BUSY", "该工作位置的单步尚未完成，请完成后再开始持续运行。")
            for item in scopes:
                if self.actor(state, item).status == "waiting":
                    self.actor(state, item).status = "ready"
            self.store.save_head(state)
            self.tasks[key] = asyncio.create_task(self.loop(run_id, reading_id))
        return self.snapshot(run_id)

    async def loop(self, run_id, reading_id):
        if reading_id is None:
            windows = list(self.load(run_id).windows)
            await asyncio.gather(*(self.scope_loop(run_id, f"window:{key}") for key in windows))
        else:
            await self.scope_loop(run_id, f"reading:{reading_id}")

    async def scope_loop(self, run_id, scope):
        try:
            while True:
                state = self.load(run_id)
                session = self.session(state, scope)
                if self.actor(state, scope).status == "waiting" and state.graph_commit <= session.get("waiting_commit", -1):
                    event = self.changes.setdefault(run_id, asyncio.Event())
                    event.clear()
                    await event.wait()
                    continue
                await self.step_scope(run_id, scope)
                await asyncio.sleep(0)
        except (KernelError, GrowthError, ProviderError):
            return

    async def pause(self, run_id, reading_id=None):
        key = (run_id, f"reading:{reading_id}" if reading_id else "growth")
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state = self.load(run_id)
        scopes = [f"reading:{reading_id}"] if reading_id else [f"window:{key}" for key in state.windows]
        for scope in scopes:
            actor = self.actor(state, scope)
            if actor.status == "calling":
                actor.status = "paused"
                session = self.session(state, scope)
                session["status"] = "paused"
                self.put(run_id, "session", session)
        self.store.save_head(state)
        return self.snapshot(run_id)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def busy(self):
        return any(not t.done() for t in self.tasks.values()) or any(lock.locked() for lock in self.locks.values())

    def recover(self):
        """Reconcile only interrupted indexed jobs, never materialize complete archives."""
        rows = self.store._db.execute(
            "SELECT o.run_id,o.value_json FROM object_heads h JOIN objects o ON o.seq=h.object_seq "
            "WHERE h.kind='request' AND json_extract(o.value_json,'$.status')='calling'"
        ).fetchall()
        for row in rows:
            request = json.loads(row["value_json"])
            state = self.load(row["run_id"])
            usage = state.config["usage"]
            estimate = request.get("reserved_tokens", 0)
            reserve = min(usage.get("reserved_tokens", 0), estimate)
            usage["reserved_tokens"] -= reserve
            usage["unknown_tokens"] += reserve
            usage["failed_calls"] += 1
            request.update(status="interrupted", error_code="REQUEST_INTERRUPTED")
            request.setdefault("attempts", []).append({"status": "interrupted", "reserved_tokens": reserve})
            scope = request["scope_id"]
            session = self.session(state, scope)
            session.update(status="paused", error={"code": "REQUEST_INTERRUPTED", "message": "中断的工作已保留，可继续。"})
            self.actor(state, scope).status = "paused"
            state.status = "paused"
            with self.store.transaction():
                self.put(state.run_id, "request", request)
                self.put(state.run_id, "session", session)
                self.store.save_head(state)
