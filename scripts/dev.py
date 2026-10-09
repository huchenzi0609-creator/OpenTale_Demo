"""Loopback-only serving, detached process ownership, and frontend builds.

Use this from an activated .venv or through ``uv run python scripts/dev.py``.
The script never reads private configuration and never forwards provider errors.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
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


def state_path(profile: str | None = None) -> Path:
    if profile not in {None, "reflexive", "growth"}:
        raise DevError("运行配置无效；请省略 --profile 或使用 reflexive、growth。")
    return STATE if profile is None else ROOT / "runtime" / profile / "server-process.json"


def frontend_directory(profile: str | None = None) -> Path:
    state_path(profile)  # Validate before constructing any profile-owned path.
    return ROOT / "frontend" / "dist" if profile is None else state_path(profile).parent / "frontend-dist"


def assert_port_free(port: int) -> None:
    try:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                probe.listen(1)
                return
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        # On macOS a reused loopback bind/listen can coexist with a wildcard
        # listener. Check reachability first, without sending application data.
        with socket.socket() as connection:
            connection.settimeout(0.2)
            result = connection.connect_ex(("127.0.0.1", port))
            if result == 0:
                raise OSError(errno.EADDRINUSE, "listener exists")
            if result != errno.ECONNREFUSED:
                raise OSError(result, "listener could not be safely excluded")
        with socket.socket() as probe:
            # Match uvicorn's restart semantics: closed connections in TIME_WAIT
            # must not look like a live server. A reused bind alone can succeed
            # beside another bound socket, so verify that listening is possible.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            probe.listen(1)
    except PermissionError:
        raise SystemExit("当前进程没有本机回环监听权限；请在允许本地服务监听的终端或环境中启动。") from None
    except OSError:
        raise SystemExit(f"端口 {port} 已被占用；请指定 --port 使用其他端口。") from None


def server_command(port: int, token: str | None = None, profile: str | None = None) -> list[str]:
    state_path(profile)
    if token:
        command = [sys.executable, str(Path(__file__).resolve()), "_child", "--port", str(port), "--token", token]
        return command + (["--profile", profile] if profile else [])
    return [
        sys.executable, "-m", "uvicorn", "opentale.api:app", "--host", "127.0.0.1",
        "--port", str(port), "--no-access-log", "--log-level", "warning",
    ]


def server_environment(port: int, profile: str | None = None) -> dict[str, str]:
    path = state_path(profile)
    env = os.environ.copy()
    # Origin checking and UI configuration must match the actual listening port.
    env["OPENTALE_PORT"] = str(port)
    if profile:
        env["OPENTALE_DATA_DIR"] = str(path.parent / "data")
        env["OPENTALE_FRONTEND_DIR"] = str(frontend_directory(profile))
    return env


def build(profile: str | None = None) -> None:
    state_path(profile)
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("未找到 npm；请先安装 Node.js。")
    env = os.environ.copy()
    env["npm_config_cache"] = str(ROOT / ".cache" / "npm")
    command = [npm, "run", "build"]
    if profile:
        command += ["--", "--outDir", str(frontend_directory(profile)), "--emptyOutDir"]
    subprocess.run(command, cwd=ROOT / "frontend", env=env, check=True)


def identity_matches(pid: int, token: str, profile: str | None = None) -> bool:
    """Never terminate a reused PID based only on a stale PID file."""
    state_path(profile)
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
    if result.returncode != 0:
        return False
    try:
        arguments = [value.strip('"') for value in shlex.split(result.stdout, posix=os.name != "nt")]
    except ValueError:
        return False
    script = str(Path(__file__).resolve())
    if script not in arguments or "_child" not in arguments:
        return False
    try:
        token_index = arguments.index("--token")
        if arguments[token_index + 1] != token:
            return False
        profile_index = arguments.index("--profile") if "--profile" in arguments else None
        actual_profile = arguments[profile_index + 1] if profile_index is not None else None
    except (ValueError, IndexError):
        return False
    return actual_profile == profile


def read_state(profile: str | None = None) -> dict | None:
    path = state_path(profile)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state.get("pid"), int) or not isinstance(state.get("token"), str):
            raise ValueError
        if state.get("profile") != profile:
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        label = "runtime/server-process.json" if profile is None else f"runtime/{profile}/server-process.json"
        raise SystemExit(f"进程记录损坏或运行配置不符；请检查 {label} 后再启动。") from None


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def detach(port: int, profile: str | None = None) -> None:
    path = state_path(profile)
    state = read_state(profile)
    if state and identity_matches(state["pid"], state["token"], profile):
        raise SystemExit(f"本脚本启动的服务仍在运行，端口 {state['port']}；请先 stop。")
    assert_port_free(port)
    if not (frontend_directory(profile) / "index.html").exists():
        build(profile)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log_path = path.parent / "server.log"
    log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    kwargs: dict = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    }
    with os.fdopen(log_fd, "ab") as log:
        process = subprocess.Popen(
            server_command(port, token, profile), cwd=ROOT, env=server_environment(port, profile),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs,
        )
    state = {"pid": process.pid, "port": port, "token": token}
    if profile:
        state["profile"] = profile
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as record:
        json.dump(state, record)
    for _ in range(100):
        if process.poll() is not None:
            path.unlink(missing_ok=True)
            label = "runtime/server.log" if profile is None else f"runtime/{profile}/server.log"
            raise SystemExit(f"服务启动失败；可本地检查 {label}，勿把原始日志公开粘贴。")
        if health(port):
            print(f"网页：http://127.0.0.1:{port}；已独立于当前终端运行。")
            print("停止：uv run python scripts/dev.py stop" + (f" --profile {profile}" if profile else ""))
            return
        time.sleep(0.1)
    print(f"服务尚未完成启动；端口 {port}，可用 status 检查。")


def stop(profile: str | None = None) -> None:
    path = state_path(profile)
    state = read_state(profile)
    if not state:
        print("没有本脚本管理的服务。")
        return
    if not identity_matches(state["pid"], state["token"], profile):
        path.unlink(missing_ok=True)
        print("旧进程记录已清理；没有终止其他进程。")
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(state["pid"]), "/T"], capture_output=True, check=False)
    else:
        os.kill(state["pid"], signal.SIGTERM)
    for _ in range(100):
        if not identity_matches(state["pid"], state["token"], profile):
            path.unlink(missing_ok=True)
            print("服务已停止。")
            return
        time.sleep(0.1)
    raise SystemExit("服务尚未退出；保留进程记录，未强制杀死或删除存档。")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "build", "serve", "detach", "stop", "status", "_child"])
    parser.add_argument("--port", type=int, default=8231)
    parser.add_argument("--profile", choices=["reflexive", "growth"],
                        help="独立的实验进程、前端及数据目录；默认保留主服务目录。")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024–65535 之间")
    if args.action == "bootstrap":
        subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap.py")], cwd=ROOT, check=True)
    elif args.action == "build":
        build(args.profile)
    elif args.action == "serve":
        assert_port_free(args.port)
        if not (frontend_directory(args.profile) / "index.html").exists():
            build(args.profile)
        subprocess.run(server_command(args.port), cwd=ROOT, env=server_environment(args.port, args.profile),
                       check=True)
    elif args.action == "_child":
        import uvicorn
        os.environ.update(server_environment(args.port, args.profile))
        uvicorn.run("opentale.api:app", host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    elif args.action == "detach":
        detach(args.port, args.profile)
    elif args.action == "stop":
        stop(args.profile)
    else:
        state = read_state(args.profile)
        if state and identity_matches(state["pid"], state["token"], args.profile):
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
