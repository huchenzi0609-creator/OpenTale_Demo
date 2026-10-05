"""Loopback, single-origin HTTP boundary; no credentials in any public projection."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .config import ConfigError, load_settings
from .domain import Limits, Record
from .kernel import KernelError
from .presets import initialize, read_presets
from .providers import ProviderError
from .runner import Runner
from .storage import StorageError, Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CreateRun(Record):
    preset_id: str = Field(min_length=1, max_length=80)
    provider: str = "fixture"
    limits: Limits = Field(default_factory=Limits)


class ForkRun(Record):
    title: str | None = Field(default=None, min_length=1, max_length=120)


class Counterfactual(Record):
    target_position_id: str = Field(min_length=1, max_length=80)
    source_position_id: str = Field(min_length=1, max_length=80)


def safe_error(code: str, message: str, status: int, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": retryable}}, status_code=status
    )


def create_app(root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    config_root = root or Path(os.environ.get("OPENTALE_ROOT", PROJECT_ROOT))
    directory = data_dir or Path(os.environ.get("OPENTALE_DATA_DIR", PROJECT_ROOT / "runtime"))
    store = Store(directory)
    settings = load_settings(config_root)
    runner = Runner(store, settings)
    store.recover()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await runner.shutdown()

    app = FastAPI(title="OpenTale", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.runner, app.state.root = store, runner, config_root

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
            if length > 65536:
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
        status = 409 if error.code in {"RUN_BUSY", "STALE_PROPOSAL"} else 422
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

    @app.exception_handler(Exception)
    async def internal_error(_: Request, __: Exception):
        return safe_error("INTERNAL_ERROR", "本地操作未完成，请检查安全诊断并重试。", 500, True)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.1.0"}

    @app.get("/api/config")
    async def config():
        return runner.settings.public()

    @app.post("/api/config/reload")
    async def reload_config():
        nonlocal settings
        if any(runner.busy(state.run.id) for state in store.list()):
            raise KernelError("RUN_BUSY", "请先暂停所有运行任务，再重新加载配置。")
        settings = load_settings(config_root)
        runner.settings = settings
        return settings.public()

    @app.get("/api/presets")
    async def presets():
        return {"presets": read_presets(PROJECT_ROOT)}

    @app.post("/api/runs")
    async def create(body: CreateRun):
        if body.provider not in {"fixture", "deepseek"}:
            raise KernelError("PROVIDER_INVALID", "请选择离线演示或 DeepSeek。")
        if body.provider == "deepseek" and not runner.settings.public()["configured"]:
            raise ProviderError("API_KEY_MISSING", "请在 .env.local 填写密钥后重新加载配置。")
        preset = next((p for p in read_presets(PROJECT_ROOT) if p["id"] == body.preset_id), None)
        if preset is None:
            raise KeyError(body.preset_id)
        state = initialize(preset, body.provider, body.limits)
        store.save(state)
        return state

    @app.get("/api/runs")
    async def runs():
        return {"runs": [s.run.model_dump() for s in store.list()]}

    @app.get("/api/runs/{run_id}")
    async def snapshot(run_id: str):
        return store.load(run_id)

    @app.post("/api/runs/{run_id}/step")
    async def step(run_id: str):
        return await runner.step(run_id)

    @app.post("/api/runs/{run_id}/start")
    async def start(run_id: str):
        return await runner.start(run_id)

    @app.post("/api/runs/{run_id}/pause")
    async def pause(run_id: str):
        return runner.pause(run_id)

    @app.post("/api/runs/{run_id}/fork")
    async def fork(run_id: str, body: ForkRun):
        if runner.busy(run_id):
            raise KernelError("RUN_BUSY", "请先暂停，再从稳定阶段建立分支。")
        state = store.fork(run_id, body.title)
        runner.event(state, "forked", from_run_id=run_id)
        return state

    @app.delete("/api/runs/{run_id}")
    async def delete(run_id: str):
        if runner.busy(run_id):
            raise KernelError("RUN_BUSY", "请先暂停该实验再删除。")
        store.delete(run_id)
        runner.locks.pop(run_id, None)
        runner.tasks.pop(run_id, None)
        runner.pause_requests.discard(run_id)
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

    distribution = PROJECT_ROOT / "frontend" / "dist"
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
