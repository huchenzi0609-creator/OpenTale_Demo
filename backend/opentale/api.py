"""Loopback, single-origin HTTP boundary; no credentials in any public projection."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .capacity import budget_bounds
from .config import ConfigError, load_settings_with_diagnostics
from .domain import Limits, Record, Snapshot
from .kernel import KernelError
from .presets import PresetDefinition, PresetError, PresetLibrary, initialize
from .providers import ProviderError
from .runner import Runner
from .security import contains_credential
from .storage import StorageError, Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CreateRun(Record):
    preset_id: str = Field(min_length=1, max_length=80)
    provider: str = "fixture"
    limits: Limits = Field(default_factory=Limits)
    mode: Literal["classic", "reflexive", "reflexive_v2"] = "classic"


class AblateMethod(Record):
    scope_id: str = Field(min_length=1, max_length=160)
    method_id: str = Field(min_length=1, max_length=160)


class ForkRun(Record):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    limits: Limits | None = None


class CreateReading(Record):
    title: str = Field(min_length=1, max_length=120)
    start_position_id: str = Field(min_length=1, max_length=80)
    enabled: bool = True


class ConfigureReading(Record):
    enabled: bool


class Counterfactual(Record):
    target_position_id: str = Field(min_length=1, max_length=80)
    source_position_id: str = Field(min_length=1, max_length=80)


class SavePreset(PresetDefinition):
    expected_version: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def safe_error(code: str, message: str, status: int, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": retryable}}, status_code=status
    )


def snapshot_response(state: Snapshot):
    """The workbench receives evidence summaries; full request material remains in explicit export."""
    if state.run.mode != "reflexive_v2":
        return state
    data = state.model_dump(exclude={
        "requests": {"__all__": {"frame": True, "attempts": {"__all__": {"projection"}}}},
        "reflection": {"object_versions"},
        "pending": {"proposal_frames", "realization_frames", "reading_frames"},
        "events": True,
    })
    hidden = {"frame", "frames", "proposal_frames", "realization_frames", "reading_frames", "reflection_frame", "initial"}
    data["events"] = [event.model_dump(exclude={"data": hidden}) for event in state.events]
    return data


def create_app(root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    config_root = root or Path(os.environ.get("OPENTALE_ROOT", PROJECT_ROOT))
    directory = data_dir or Path(os.environ.get("OPENTALE_DATA_DIR", PROJECT_ROOT / "runtime"))
    store = Store(directory)
    settings, configuration_diagnostics = load_settings_with_diagnostics(config_root)
    runner = Runner(store, settings)
    from .growth_runner import GrowthRunner
    growth_runner = GrowthRunner(directory / "growth", settings)
    library = PresetLibrary(PROJECT_ROOT / "presets", directory / "presets")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.recover()
        growth_runner.recover()
        yield
        await runner.shutdown()
        await growth_runner.shutdown()

    app = FastAPI(title="OpenTale", version="0.6.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.runner, app.state.root = store, runner, config_root
    app.state.presets = library
    app.state.growth_runner = growth_runner
    from .growth_api import register_graph_routes
    register_graph_routes(app, growth_runner, library)

    @app.middleware("http")
    async def origin_and_size(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    allowed = (
                        parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                        and parsed.port in {settings.port, 5196}
                    )
                except ValueError:
                    allowed = False
                if not allowed:
                    return safe_error("ORIGIN_REJECTED", "仅允许本地网页操作。", 403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return safe_error("REQUEST_INVALID", "请求格式无效。", 400)
            bound = 1048576 if request.url.path.startswith(("/api/presets", "/api/graph-presets", "/api/graph-runs")) else 65536
            if length < 0 or length > bound:
                return safe_error("REQUEST_TOO_LARGE", "请求内容过大。", 413)
            if len(await request.body()) > bound:
                return safe_error("REQUEST_TOO_LARGE", "请求内容过大。", 413)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError):
        return safe_error("REQUEST_INVALID", "请求参数无效，请检查预设、预算及标识。", 422)

    @app.exception_handler(KeyError)
    async def not_found(_: Request, __: KeyError):
        return safe_error("NOT_FOUND", "预设或存档不存在。", 404)

    @app.exception_handler(KernelError)
    async def kernel_error(_: Request, error: KernelError):
        status = 404 if error.code == "RUN_NOT_FOUND" else 409 if error.code in {
            "RUN_BUSY", "STALE_PROPOSAL", "PRESET_CONFLICT", "SCOPE_CONFLICT"
        } else 422
        return safe_error(error.code, error.message, status, error.retryable)

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, error: ProviderError):
        return safe_error(
            error.code, error.message, 503 if error.code == "API_KEY_MISSING" else 422, error.retryable
        )

    @app.exception_handler(ConfigError)
    async def config_error(_: Request, error: ConfigError):
        return safe_error(error.code, error.message, 422)

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, __: StorageError):
        return safe_error("STORAGE_INVALID", "存档无法安全加载；原文件保持不动。", 409)

    @app.exception_handler(PresetError)
    async def preset_error(_: Request, error: PresetError):
        status = (
            404
            if error.code == "PRESET_NOT_FOUND"
            else 409
            if error.code in {"PRESET_CONFLICT", "PRESET_READ_ONLY"}
            else 422
        )
        return safe_error(error.code, error.message, status, error.retryable)

    @app.exception_handler(Exception)
    async def internal_error(_: Request, __: Exception):
        return safe_error("INTERNAL_ERROR", "本地操作未完成，请检查安全诊断并重试。", 500, True)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.6.0"}

    @app.get("/api/config")
    async def config():
        return {
            **runner.settings.public(), "diagnostics": configuration_diagnostics,
            "budget_bounds": budget_bounds(),
        }

    @app.post("/api/config/reload")
    async def reload_config():
        nonlocal settings, configuration_diagnostics
        if growth_runner.busy() or runner.any_busy():
            raise KernelError("RUN_BUSY", "请先暂停所有运行任务，再重新加载配置。")
        # Publish settings and their safe provenance from the same successful read.
        settings, configuration_diagnostics = load_settings_with_diagnostics(config_root)
        runner.settings = settings
        growth_runner.settings = settings
        app.state.graph_presets.credential = settings.api_key.get_secret_value()
        return {
            **settings.public(), "diagnostics": configuration_diagnostics,
            "budget_bounds": budget_bounds(),
        }

    @app.get("/api/presets")
    async def presets():
        catalog = library.list()
        # Reject publication of an accidental credential pasted into experiment materials.
        response = {"presets": catalog, "errors": library.errors}
        if contains_credential(response, runner.settings.api_key.get_secret_value()):
            raise KernelError("PRIVATE_DATA_REJECTED", "预设包含不能公开的配置内容，请在本地移除后重试。")
        return response

    def save_definition(body: SavePreset):
        if contains_credential(body.model_dump(), runner.settings.api_key.get_secret_value()):
            raise KernelError("PRIVATE_DATA_REJECTED", "密钥只能填写到 .env.local，不能保存在人物或表达中。")
        definition = PresetDefinition.model_validate(body.model_dump(exclude={"expected_version"}))
        return {"preset": library.save(definition, expected_version=body.expected_version)}

    @app.post("/api/presets")
    async def create_preset(body: SavePreset):
        if body.expected_version is not None:
            raise KernelError("PRESET_INVALID", "新建预设不能携带编辑版本。")
        return save_definition(body)

    @app.put("/api/presets/{preset_id}")
    async def update_preset(preset_id: str, body: SavePreset):
        if body.id != preset_id or body.expected_version is None:
            raise KernelError("PRESET_INVALID", "编辑须保留预设标识并携带当前版本。")
        return save_definition(body)

    @app.post("/api/runs")
    async def create(body: CreateRun):
        if body.provider not in {"fixture", "deepseek"}:
            raise KernelError("PROVIDER_INVALID", "请选择离线演示或 DeepSeek。")
        if body.provider == "deepseek" and not runner.settings.public()["configured"]:
            raise ProviderError("API_KEY_MISSING", "请在 .env.local 填写密钥后重新加载配置。")
        preset = library.get(body.preset_id)
        if contains_credential(preset, runner.settings.api_key.get_secret_value()):
            raise KernelError("PRIVATE_DATA_REJECTED", "请先在本地移除预设中误填的配置内容。")
        state = initialize(preset, body.provider, body.limits, body.mode)
        store.save(state)
        return snapshot_response(state)

    @app.get("/api/runs")
    async def runs():
        return {"runs": [s.run.model_dump() for s in store.list()]}

    @app.get("/api/runs/{run_id}")
    async def snapshot(run_id: str):
        return snapshot_response(store.load(run_id))

    @app.get("/api/runs/{run_id}/reflection")
    async def reflection(run_id: str):
        state = store.load(run_id)
        return {
            "mode": state.run.mode,
            **(state.reflection.model_dump() if state.reflection else {
                "methods": [], "adoptions": {}, "choices": [],
            }),
            "note": "程序层面的实际选择与方法版本；不包含CoT，也不证明真实模型已经产生叙事意图。",
        }

    def visible_objects(run_id: str, scope_id: str):
        from .reflection import objects_for, position_scope, reading_scope

        state = store.load(run_id)
        allowed = {position_scope(position.id) for position in state.positions}
        allowed |= {reading_scope(track.id) for track in state.reading_tracks}
        if state.reflection is None or scope_id not in allowed:
            raise KernelError("METHOD_SCOPE_INVALID", "请选择当前反身实验中的有效位置或阅读。")
        return objects_for(state, scope_id)

    @app.get("/api/runs/{run_id}/objects")
    async def objects(run_id: str, scope_id: str, query: str = ""):
        catalog = visible_objects(run_id, scope_id)
        from .retrieval import retrieve

        if query.strip():
            # Query uses the same deterministic, permission-filtered full-material index as methods.
            found = retrieve(catalog, query)
            rows = [catalog[ref] for ref in found]
        else:
            rows = list(catalog.values())
        previews = [{**{key: row[key] for key in ("id", "kind", "title", "role", "version", "language_version")
                       if key in row}, "text": row.get("text", "")[:800],
                     "preview_truncated": len(row.get("text", "")) > 800} for row in rows]
        return {"scope_id": scope_id, "query": query, "objects": previews, "total": len(rows)}

    @app.get("/api/runs/{run_id}/object")
    async def full_object(run_id: str, scope_id: str, object_id: str):
        catalog = visible_objects(run_id, scope_id)
        if object_id not in catalog:
            raise KernelError("METHOD_INPUT_UNAVAILABLE", "该对象在此位置不可读取。")
        return {"scope_id": scope_id, "object": catalog[object_id]}

    @app.post("/api/runs/{run_id}/experiments")
    async def paired_experiment(run_id: str, body: AblateMethod):
        from uuid import uuid4

        from .kernel import fingerprint, semantic_projection
        from .request_evidence import execution_signature

        original = runner._stable(run_id)
        if original.run.mode != "reflexive_v2" or original.pending is not None:
            raise KernelError("EXPERIMENT_NOT_READY", "请从新版反身实验已完成的窗口建立对照。")
        if original.reflection is None or body.method_id not in original.reflection.adoptions.get(body.scope_id, []):
            raise KernelError("METHOD_REFERENCE_INVALID", "请选择当前已采用的方法建立对照。")
        manifest = {
            "id": uuid4().hex, "origin_run_id": run_id, "origin_revision": original.run.revision,
            "origin_digest": fingerprint(semantic_projection(original)),
            "policy_revision": original.policy_revision,
            "scope_id": body.scope_id, "method_id": body.method_id,
            "intervention": "release_method", "compiler_version": "views-v2",
            "model": runner.settings.model if original.run.provider == "deepseek" else "fixture",
            "max_output_tokens": runner.settings.max_output_tokens,
            "context_target_chars": getattr(runner.settings, "context_target_chars", 96000),
            "execution_signature": execution_signature(original.run.provider, runner.settings),
            "ancestor_usage": original.run.usage.model_dump(),
            "note": "共同恢复点与首次干预已固定；后续轨迹允许分化，单次文本差异不证明语义因果。",
        }
        control = store.fork(run_id, f"{original.run.title} · 对照")
        intervention = store.fork(run_id, f"{original.run.title} · 方法撤去")
        control.experiment = {**manifest, "arm": "control"}
        intervention.experiment = {**manifest, "arm": "intervention"}
        runner.event(control, "experiment_created", manifest=control.experiment)
        runner.event(intervention, "experiment_created", manifest=intervention.experiment)
        intervention = runner.release_method(intervention.run.id, body.scope_id, body.method_id)
        return {"control": snapshot_response(control), "intervention": snapshot_response(intervention), "manifest": manifest}

    @app.post("/api/runs/{run_id}/reflection/ablate")
    async def ablate(run_id: str, body: AblateMethod):
        from .reflection import ablate_method

        state = runner._stable(run_id)
        if state.pending is not None:
            raise KernelError("RUN_BUSY", "请先完成当前恢复点，再对稳定世界做离线消融。")
        return ablate_method(state, body.scope_id, body.method_id)

    @app.post("/api/runs/{run_id}/reflection/release")
    async def release_method(run_id: str, body: AblateMethod):
        return snapshot_response(runner.release_method(run_id, body.scope_id, body.method_id))

    @app.patch("/api/runs/{run_id}/limits")
    async def update_limits(run_id: str, body: Limits):
        return snapshot_response(runner.update_limits(run_id, body))

    @app.post("/api/runs/{run_id}/readings")
    async def create_reading(run_id: str, body: CreateReading):
        if contains_credential(body.model_dump(), runner.settings.api_key.get_secret_value()):
            raise KernelError("PRIVATE_DATA_REJECTED", "密钥只能填写到本机配置文件，不能用于阅读配置。")
        return snapshot_response(runner.add_reading(run_id, body.title, body.start_position_id, body.enabled))

    @app.patch("/api/runs/{run_id}/readings/{reading_id}")
    async def configure_reading(run_id: str, reading_id: str, body: ConfigureReading):
        return snapshot_response(runner.set_reading_enabled(run_id, reading_id, body.enabled))

    @app.post("/api/runs/{run_id}/readings/{reading_id}/present")
    async def present_reading(run_id: str, reading_id: str):
        return snapshot_response(await runner.present_reading(run_id, reading_id))

    @app.post("/api/runs/{run_id}/step")
    async def step(run_id: str):
        return snapshot_response(await runner.step(run_id))

    @app.post("/api/runs/{run_id}/start")
    async def start(run_id: str):
        return snapshot_response(await runner.start(run_id))

    @app.post("/api/runs/{run_id}/pause")
    async def pause(run_id: str):
        return snapshot_response(runner.pause(run_id))

    @app.post("/api/runs/{run_id}/fork")
    async def fork(run_id: str, body: ForkRun):
        if runner.busy(run_id):
            raise KernelError("RUN_BUSY", "请先暂停，再从稳定阶段建立分支。")
        state = store.fork(run_id, body.title, limits=body.limits)
        runner.event(state, "forked", from_run_id=run_id)
        return snapshot_response(state)

    @app.delete("/api/runs/{run_id}")
    async def delete(run_id: str):
        if runner.busy(run_id):
            raise KernelError("RUN_BUSY", "请先暂停该实验再删除。")
        store.delete(run_id)
        runner.locks.pop(run_id, None)
        runner.tasks.pop(run_id, None)
        runner.pause_requests.discard(run_id)
        runner._funded_jobs.pop(run_id, None)
        return {"deleted": True}

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        state = store.load(run_id)
        return Response(
            state.model_dump_json(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="opentale-{state.run.id}.json"'},
        )

    @app.get("/api/runs/{run_id}/replay")
    async def replay(run_id: str):
        from .replay import verify_replay

        if runner.busy(run_id):
            raise KernelError("RUN_BUSY", "请先暂停，再验证稳定阶段的重放。")
        return verify_replay(store.load(run_id), store.read_events(run_id))

    @app.post("/api/runs/{run_id}/counterfactual")
    async def counterfactual(run_id: str, body: Counterfactual):
        return await runner.counterfactual(run_id, body.target_position_id, body.source_position_id)

    @app.get("/api/verify")
    async def verify():
        from .verification import verify_mechanisms

        return await verify_mechanisms(settings)

    distribution = Path(os.environ.get("OPENTALE_FRONTEND_DIR", PROJECT_ROOT / "frontend" / "dist"))
    if distribution.is_dir():
        assets = distribution / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(distribution / "index.html")
    else:

        @app.get("/")
        async def not_built():
            return {"message": "请先构建网页：.venv/bin/python scripts/dev.py build", "api": "/api/health"}

    return app


app = create_app()
