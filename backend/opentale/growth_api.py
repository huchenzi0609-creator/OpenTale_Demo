"""Opt-in graph routes; legacy snapshots and endpoints retain their contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from .domain import Limits, Record
from .growth_kernel import GrowthError
from .growth_presets import GrowthPresetLibrary
from .kernel import KernelError


class WindowInput(Record):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(default="", max_length=120)
    seed: str = ""
    direction: str = ""


class CreateGraph(Record):
    title: str = Field(default="生长与遍历实验", min_length=1, max_length=120)
    provider: str = "fixture"
    preset_id: str | None = None
    windows: list[WindowInput] | None = None
    shared_seed: str = ""
    limits: Limits = Field(default_factory=lambda: Limits(max_calls=None, max_tokens=None, max_windows=None))


class NewReading(Record):
    title: str = Field(min_length=1, max_length=120)
    entry_node_ids: list[str] | None = None
    start_node_id: str | None = None
    direction: str = ""


class SaveGraphPreset(Record):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    windows: list[WindowInput] = Field(min_length=1)
    shared_seed: str = ""
    expected_version: str | None = None


class LegacyImport(Record):
    legacy_run_id: str
    window_id: str
    source_kind: str
    source_id: str
    reading_id: str = "main"


class ForkGraph(Record):
    title: str | None = None


def register_graph_routes(app, runner, library):
    presets = GrowthPresetLibrary(runner.store.path.parent / "presets", runner.settings.api_key.get_secret_value(),
                                 Path(__file__).resolve().parents[2] / "examples" / "growth-presets")
    app.state.graph_presets = presets

    @app.get("/api/graph-presets")
    async def graph_presets():
        presets.credential = runner.settings.api_key.get_secret_value()
        return {"presets": presets.list()}

    @app.get("/api/graph-presets/{preset_id}")
    async def graph_preset(preset_id: str):
        presets.credential = runner.settings.api_key.get_secret_value()
        return presets.get(preset_id)

    @app.post("/api/graph-presets")
    async def save_graph_preset(body: SaveGraphPreset):
        presets.credential = runner.settings.api_key.get_secret_value()
        return presets.save(body.model_dump(exclude={"expected_version"}), body.expected_version)
    @app.exception_handler(GrowthError)
    async def growth_error(_, exc):
        from .api import safe_error
        return safe_error(exc.code, exc.message, 404 if exc.code == "RUN_NOT_FOUND" else 422)

    @app.get("/api/graph-runs")
    async def list_graphs(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        return runner.ensure_safe({"items": [{**row, "id": row["run_id"]}
                                 for row in runner.store.list_runs(offset=offset, limit=limit)]})

    @app.post("/api/graph-runs")
    async def create_graph(body: CreateGraph):
        if body.provider == "deepseek" and not runner.settings.api_key.get_secret_value():
            from .providers.base import ProviderError
            raise ProviderError("API_KEY_MISSING", "请先配置本地密钥并重新加载配置。")
        shared = body.shared_seed
        windows = [w.model_dump() for w in body.windows] if body.windows else None
        if body.preset_id and not windows:
            if body.preset_id.startswith("graph:"):
                presets.credential = runner.settings.api_key.get_secret_value()
                preset = presets.get(body.preset_id[6:])
                windows = preset["windows"]
                shared = shared or preset["shared_seed"]
            else:
                preset = library.get(body.preset_id)
                windows = [{"id": p["id"], "label": p["name"], "seed": p["seed"]} for p in preset["positions"]]
                shared = shared or "\n\n".join(e["text"] for e in preset["expressions"])
        if windows is None:
            windows = [
                {"id": "arrival", "label": "归来的人", "seed": "她回到熟悉的屋子，发现有人重新使用了她留下的东西。"},
                {"id": "staying", "label": "留下的人", "seed": "他一直住在这里，记得这些东西在另一种生活里的用途。"},
            ]
            shared = shared or "两个人可以从同一封尚未拆开的信继续展开生活。"
        return runner.create(title=body.title, provider=body.provider, windows=windows, shared_seed=shared,
                             limits=body.limits, preset_id=body.preset_id or "custom")

    @app.get("/api/graph-runs/{run_id}")
    async def get_graph(run_id: str):
        return runner.snapshot(run_id)

    @app.get("/api/graph-runs/{run_id}/status")
    async def graph_status(run_id: str):
        snapshot = runner.snapshot(run_id)
        return {key: snapshot[key] for key in ("run", "graph_commit", "counts", "active_scopes", "sessions")}

    @app.post("/api/graph-runs/{run_id}/limits")
    async def graph_limits(run_id: str, body: Limits):
        if runner.busy():
            raise KernelError("RUN_BUSY", "请先暂停工作，再修改预算。")
        state = runner.load(run_id)
        state.config["limits"] = body.model_dump()
        runner.store.save_head(state)
        return runner.snapshot(run_id)

    @app.post("/api/graph-runs/{run_id}/fork")
    async def fork_graph(run_id: str, body: ForkGraph):
        if runner.busy():
            raise KernelError("RUN_BUSY", "请先暂停工作，再复制稳定状态。")
        runner.ensure_safe(body.model_dump())
        state = runner.store.fork_run(run_id, title=body.title)
        return runner.snapshot(state.run_id)

    @app.delete("/api/graph-runs/{run_id}")
    async def delete_graph(run_id: str):
        if runner.busy():
            raise KernelError("RUN_BUSY", "请先暂停工作，再删除存档。")
        runner.load(run_id)
        return {"deleted": runner.store.delete_run(run_id)}

    @app.get("/api/graph-runs/{run_id}/export")
    async def export_graph(run_id: str):
        runner.load(run_id)
        def records():
            for row in runner.store.iter_export(run_id):
                yield json.dumps(runner.ensure_safe(row), ensure_ascii=False) + "\n"
        return StreamingResponse(records(), media_type="application/x-ndjson")

    @app.post("/api/graph-runs/{run_id}/imports")
    async def import_legacy(run_id: str, body: LegacyImport):
        from .growth_kernel import add_node
        from .growth_types import ObjectRef
        if runner.busy() or app.state.runner.busy(body.legacy_run_id):
            raise KernelError("RUN_BUSY", "请先暂停来源和目标实验，再导入。")
        legacy = app.state.store.load(body.legacy_run_id)
        sources = {"seed": [(p.id, p.seed) for p in legacy.positions],
                   "expression": [(e.id, e.text) for e in legacy.expressions],
                   "continuation": [(c.position_id, c.surface) for c in legacy.continuations],
                   "story": [(s.id, s.text) for track in legacy.reading_tracks if track.id == body.reading_id
                             for s in track.story]}
        text = next((text for key, text in sources.get(body.source_kind, []) if key == body.source_id), None)
        if not text:
            raise KernelError("TARGET_UNKNOWN", "所选旧档材料不存在。")
        runner.ensure_safe(text)
        state = runner.load(run_id)
        provenance = runner.put(run_id, "import_source", {"id": "import-" + hashlib.sha256(
            json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()[:24],
            "legacy_run_id": body.legacy_run_id, "kind": body.source_kind, "source_id": body.source_id,
            "reading_id": body.reading_id, "text_ref": runner.store.put_text(text).model_dump()})
        add_node(runner.store, state, body.window_id, text, request_id=provenance["id"], input_refs=[
            ObjectRef(kind="import_source", object_id=provenance["id"], version=provenance["version"])])
        return runner.snapshot(run_id)

    @app.get("/api/graph-runs/{run_id}/graph")
    async def graph(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                    at_commit: int | None = Query(None, ge=0)):
        state = runner.load(run_id)
        nodes = runner.rows(state, "node", owner=True, offset=offset, limit=limit, horizon=at_commit)
        links = runner.rows(state, "link", owner=True, offset=offset, limit=limit, horizon=at_commit)
        return runner.ensure_safe({"nodes": nodes, "links": links, "graph_commit": state.graph_commit,
                "next_offset": offset + limit if len(nodes) == limit or len(links) == limit else None})

    @app.get("/api/graph-runs/{run_id}/nodes/{node_id}")
    async def graph_node(run_id: str, node_id: str, version: str | None = None):
        runner.load(run_id)
        node = runner.store.get(run_id, "node", node_id, version=version, include_private=True)
        if node is None:
            raise KernelError("RUN_NOT_FOUND", "叙事节点不存在。")
        return runner.ensure_safe({"node": node, "text": runner.body(node)})

    @app.post("/api/graph-runs/{run_id}/growth/step")
    async def growth_step(run_id: str):
        return await runner.step(run_id)

    @app.post("/api/graph-runs/{run_id}/growth/start")
    async def growth_start(run_id: str):
        runner.load(run_id)
        return await runner.start(run_id)

    @app.post("/api/graph-runs/{run_id}/growth/pause")
    async def growth_pause(run_id: str):
        return await runner.pause(run_id)

    @app.post("/api/graph-runs/{run_id}/readings")
    async def add_reading(run_id: str, body: NewReading):
        return runner.add_reading(run_id, body.title, body.entry_node_ids
                                  or ([body.start_node_id] if body.start_node_id else None), body.direction)

    @app.post("/api/graph-runs/{run_id}/readings/{track_id}/step")
    async def reading_step(run_id: str, track_id: str):
        return await runner.step(run_id, track_id)

    @app.post("/api/graph-runs/{run_id}/readings/{track_id}/start")
    async def reading_start(run_id: str, track_id: str):
        runner.actor(runner.load(run_id), f"reading:{track_id}")
        return await runner.start(run_id, track_id)

    @app.post("/api/graph-runs/{run_id}/readings/{track_id}/pause")
    async def reading_pause(run_id: str, track_id: str):
        return await runner.pause(run_id, track_id)

    @app.get("/api/graph-runs/{run_id}/readings/{track_id}/{collection}")
    async def reading_collection(run_id: str, track_id: str, collection: str,
                                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        state = runner.load(run_id)
        kind = {"visits": "visit", "passages": "passage"}.get(collection)
        if kind is None:
            raise KernelError("ACTION_INVALID", "记录类型不存在。")
        rows = runner.rows(state, kind, f"reading:{track_id}", offset=offset, limit=limit)
        return runner.ensure_safe({"items": [runner.expand(state, kind, r) for r in rows if r["track_id"] == track_id],
                "next_offset": offset + limit if len(rows) == limit else None})

    @app.get("/api/graph-runs/{run_id}/{collection}")
    async def graph_collection(run_id: str, collection: str, offset: int = Query(0, ge=0),
                               limit: int = Query(100, ge=1, le=100)):
        state = runner.load(run_id)
        kind = {"methods": "method", "trials": "trial", "responses": "response", "events": "event",
                "receipts": "receipt", "sessions": "session"}.get(collection)
        if kind is None:
            raise KernelError("ACTION_INVALID", "记录类型不存在。")
        rows = runner.rows(state, kind, owner=True, offset=offset, limit=limit)
        return runner.ensure_safe({"items": [runner.expand(state, kind, row) for row in rows],
                "next_offset": offset + limit if len(rows) == limit else None})
