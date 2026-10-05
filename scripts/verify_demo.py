"""Exercise an isolated, real HTTP fixture lifecycle; never call a model API."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VerificationFailure(Exception):
    pass


def check(condition: bool, label: str) -> None:
    if not condition:
        raise VerificationFailure(label)
    print(f"PASS {label}")


class Server:
    def __init__(self, directory: Path):
        self.directory = directory
        self.process: subprocess.Popen | None = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]

    def start(self) -> None:
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("OPENTALE_"):
                del env[name]
        env["OPENTALE_ROOT"] = str(self.directory)
        env["OPENTALE_DATA_DIR"] = str(self.directory / "data")
        env["OPENTALE_PORT"] = str(self.port)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "opentale.api:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--no-access-log", "--log-level", "critical"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise VerificationFailure("隔离服务启动失败（未读取或输出日志）")
            try:
                if self.request("GET", "/api/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise VerificationFailure("隔离服务健康检查超时")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())


def id_of(snapshot: dict) -> str:
    return snapshot["run"]["id"]


def assert_safe_projection(value: dict) -> None:
    forbidden = {"api_key", "authorization", "reasoning_content", "base_url", "raw_response"}

    def visit(node: object) -> bool:
        if isinstance(node, dict):
            return all(str(key).lower() not in forbidden and visit(item) for key, item in node.items())
        if isinstance(node, list):
            return all(visit(item) for item in node)
        return True

    check(visit(value), "公开投影不含配置密钥、请求头或推理字段")


def verify(server: Server) -> None:
    status, health = server.request("GET", "/api/health")
    check(status == 200 and health.get("ok") is True, "真实 HTTP 健康检查")
    status, config = server.request("GET", "/api/config")
    check(status == 200 and config.get("configured") is False, "隔离配置不加载用户密钥")
    assert_safe_projection(config)
    status, verification = server.request("GET", "/api/verify")
    check(status == 200 and verification.get("ok") is True and bool(verification.get("checks")) and all(
        item.get("passed") is True for item in verification.get("checks", [])
    ), "内置离线机制验收")
    status, run = server.request("POST", "/api/runs", {"preset_id": "letters", "provider": "fixture"})
    check(status == 200 and run["run"]["revision"] == 0, "创建 fixture 对局")
    run_id = id_of(run)
    status, advanced = server.request("POST", f"/api/runs/{run_id}/step")
    check(status == 200 and advanced["run"]["revision"] == 1 and bool(advanced["story"]), "联合安装、续延与呈现完成一窗口")
    check(bool(advanced["audit"]["joint_targets"]), "多个来源共同影响活跃目标")
    check(any(event["type"] == "batch_installed" for event in advanced["events"]), "阶段事件记录包含联合安装")
    assert_safe_projection(advanced)
    candidate = next((
        (use["position_id"], binding["source_position_id"])
        for use in advanced["uses"] for binding in use["bindings"]
        if binding["source_position_id"] != use["position_id"]
    ), None)
    check(candidate is not None, "存在真实跨位置使用关系")
    status, counterfactual = server.request("POST", f"/api/runs/{run_id}/counterfactual", {
        "target_position_id": candidate[0], "source_position_id": candidate[1],
    })
    check(status == 200 and counterfactual.get("structural_effect") is True
          and counterfactual.get("semantic_fixture_effect") is True, "反事实消融改变条件及 fixture 续写")
    _, after_counterfactual = server.request("GET", f"/api/runs/{run_id}")
    check(after_counterfactual == advanced, "离线消融不修改原对局")
    status, replay = server.request("GET", f"/api/runs/{run_id}/replay")
    check(status == 200 and replay.get("ok") is True and bool(replay.get("checks"))
          and all(item.get("passed") is True for item in replay["checks"]), "语义事件重新执行并与 E/U/K 及阅读状态相等")
    status, fork = server.request("POST", f"/api/runs/{run_id}/fork", {"title": "离线验收分支"})
    check(status == 200 and id_of(fork) != run_id and fork["run"]["fork_of"] == run_id, "独立分支保留来源")
    _, fork_advanced = server.request("POST", f"/api/runs/{id_of(fork)}/step")
    _, unchanged = server.request("GET", f"/api/runs/{run_id}")
    check(unchanged == advanced and fork_advanced["run"]["revision"] == 2, "分支继续不改变原对局")
    server.stop()
    server.start()
    status, restored = server.request("GET", f"/api/runs/{run_id}")
    check(status == 200 and restored == advanced, "服务重启后完整存档恢复")
    status, replay_restored = server.request("GET", f"/api/runs/{run_id}/replay")
    check(status == 200 and replay_restored.get("ok") is True
          and replay_restored.get("digest") == replay.get("digest"), "持久化事件重放摘要稳定")
    status, exported = server.request("GET", f"/api/runs/{run_id}/export")
    check(status == 200 and exported["run"]["id"] == run_id, "导出可解析的对局快照")
    assert_safe_projection(exported)
    status, limited = server.request("POST", "/api/runs", {
        "preset_id": "letters", "provider": "fixture",
        "limits": {"max_calls": 1, "max_tokens": 1000, "max_parallel": 1, "max_windows": 1},
    })
    check(status == 200, "创建严格预算对局")
    server.request("POST", f"/api/runs/{id_of(limited)}/step")
    _, exhausted = server.request("GET", f"/api/runs/{id_of(limited)}")
    check(exhausted["run"]["status"] == "budget_exhausted"
          and exhausted["run"]["usage"]["calls"] <= 1, "调用前预算准入与耗尽暂停")
    status, background = server.request("POST", "/api/runs", {"preset_id": "letters", "provider": "fixture"})
    check(status == 200, "创建后台控制对局")
    server.request("POST", f"/api/runs/{id_of(background)}/start")
    server.request("POST", f"/api/runs/{id_of(background)}/pause")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, paused = server.request("GET", f"/api/runs/{id_of(background)}")
        if paused["run"]["status"] != "running":
            break
        time.sleep(0.05)
    check(paused["run"]["status"] != "running", "后台开始与协作式暂停")
    for snapshot in [background, limited, fork, advanced]:
        status, deleted = server.request("DELETE", f"/api/runs/{id_of(snapshot)}")
        check(status == 200 and deleted.get("deleted") is True, "删除指定非活跃存档")
    _, listing = server.request("GET", "/api/runs")
    check(listing.get("runs") == [], "删除后目录索引为空")


def main() -> None:
    print("开始隔离离线 HTTP 验收：仅 fixture；不读取用户配置，不调用付费模型。")
    with tempfile.TemporaryDirectory(prefix="opentale-verify-") as directory:
        isolated = Path(directory)
        server: Server | None = None
        try:
            server = Server(isolated)
            server.start()
            verify(server)
        except VerificationFailure as error:
            print(f"FAIL：{error}")
            raise SystemExit(1) from None
        except (KeyError, ValueError, OSError):
            print("FAIL：离线验收未通过；输出仅含安全检查标签，不包含服务原始日志。")
            raise SystemExit(1) from None
        finally:
            if server:
                server.stop()
    print("离线 HTTP 生命周期通过；这不构成真实模型的语义或文学性验收。")


if __name__ == "__main__":
    main()
