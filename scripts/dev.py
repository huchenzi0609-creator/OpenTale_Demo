"""Loopback-only serving, detached process ownership, and frontend builds.

Use this from an activated .venv or through ``uv run python scripts/dev.py``.
The script never reads private configuration and never forwards provider errors.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "runtime" / "server-process.json"


class DevError(Exception):
    """Safe developer-facing failure without subprocess details or exception chains."""


def assert_port_free(port: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except PermissionError:
            raise SystemExit("当前进程没有本机回环监听权限；请在允许本地服务监听的终端或环境中启动。") from None
        except OSError:
            raise SystemExit(f"端口 {port} 已被占用；请指定 --port 使用其他端口。") from None


def server_command(port: int, token: str | None = None) -> list[str]:
    if token:
        return [sys.executable, str(Path(__file__).resolve()), "_child", "--port", str(port), "--token", token]
    return [
        sys.executable, "-m", "uvicorn", "opentale.api:app", "--host", "127.0.0.1",
        "--port", str(port), "--no-access-log", "--log-level", "warning",
    ]


def server_environment(port: int) -> dict[str, str]:
    env = os.environ.copy()
    # Origin checking and UI configuration must match the actual listening port.
    env["OPENTALE_PORT"] = str(port)
    return env


def build() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("未找到 npm；请先安装 Node.js。")
    env = os.environ.copy()
    env["npm_config_cache"] = str(ROOT / ".cache" / "npm")
    subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", env=env, check=True)


def identity_matches(pid: int, token: str) -> bool:
    """Never terminate a reused PID based only on a stale PID file."""
    message = "无法验证服务进程身份：当前环境受权限限制或缺少进程检查工具。请使用有权限的本地终端；进程记录已保留。"
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False, timeout=5,
            )
    except (OSError, subprocess.SubprocessError):
        raise DevError(message) from None
    if result.returncode != 0 and result.stderr.strip():
        raise DevError(message) from None
    return result.returncode == 0 and str(Path(__file__).resolve()) in result.stdout and token in result.stdout


def read_state() -> dict | None:
    if not STATE.exists():
        return None
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state.get("pid"), int) or not isinstance(state.get("token"), str):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        raise SystemExit("进程记录损坏；请检查 runtime/server-process.json 后再启动。") from None


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def detach(port: int) -> None:
    state = read_state()
    if state and identity_matches(state["pid"], state["token"]):
        raise SystemExit(f"本脚本启动的服务仍在运行，端口 {state['port']}；请先 stop。")
    assert_port_free(port)
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        build()
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log_path = STATE.parent / "server.log"
    log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    kwargs: dict = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    }
    with os.fdopen(log_fd, "ab") as log:
        process = subprocess.Popen(
            server_command(port, token), cwd=ROOT, env=server_environment(port),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs,
        )
    state = {"pid": process.pid, "port": port, "token": token}
    descriptor = os.open(STATE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as record:
        json.dump(state, record)
    for _ in range(100):
        if process.poll() is not None:
            STATE.unlink(missing_ok=True)
            raise SystemExit("服务启动失败；可本地检查 runtime/server.log，勿把原始日志公开粘贴。")
        if health(port):
            print(f"网页：http://127.0.0.1:{port}；已独立于当前终端运行。")
            print("停止：uv run python scripts/dev.py stop")
            return
        time.sleep(0.1)
    print(f"服务尚未完成启动；端口 {port}，可用 status 检查。")


def stop() -> None:
    state = read_state()
    if not state:
        print("没有本脚本管理的服务。")
        return
    if not identity_matches(state["pid"], state["token"]):
        STATE.unlink(missing_ok=True)
        print("旧进程记录已清理；没有终止其他进程。")
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(state["pid"]), "/T"], capture_output=True, check=False)
    else:
        os.kill(state["pid"], signal.SIGTERM)
    for _ in range(100):
        if not identity_matches(state["pid"], state["token"]):
            STATE.unlink(missing_ok=True)
            print("服务已停止。")
            return
        time.sleep(0.1)
    raise SystemExit("服务尚未退出；保留进程记录，未强制杀死或删除存档。")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "build", "serve", "detach", "stop", "status", "_child"])
    parser.add_argument("--port", type=int, default=8231)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024–65535 之间")
    if args.action == "bootstrap":
        subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap.py")], cwd=ROOT, check=True)
    elif args.action == "build":
        build()
    elif args.action == "serve":
        assert_port_free(args.port)
        if not (ROOT / "frontend" / "dist" / "index.html").exists():
            build()
        subprocess.run(server_command(args.port), cwd=ROOT, env=server_environment(args.port), check=True)
    elif args.action == "_child":
        import uvicorn
        os.environ["OPENTALE_PORT"] = str(args.port)
        uvicorn.run("opentale.api:app", host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    elif args.action == "detach":
        detach(args.port)
    elif args.action == "stop":
        stop()
    else:
        state = read_state()
        if state and identity_matches(state["pid"], state["token"]):
            print(f"服务进程存在；端口 {state['port']}；健康检查：{'正常' if health(state['port']) else '未响应'}")
        else:
            print("没有本脚本管理的服务在运行。")


def main() -> None:
    try:
        _main()
    except DevError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
