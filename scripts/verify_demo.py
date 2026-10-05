"""Exercise an isolated, real HTTP fixture lifecycle; never call a model API."""

from __future__ import annotations

import json
import os
import socket
import stat
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


def verify_custom_presets(server: Server) -> list[dict]:
    """Only inspect this verifier's own isolated, generated preset materials."""
    draft = {
        "id": "verification-river",
        "name": "河边的几处生活",
        "description": "仅用于隔离 HTTP 验收的自定义材料。",
        "positions": [
            {"id": f"person_{index}", "name": f"河边人{index}", "seed": f"# 人物 {index}\n\n她记得河水涨过台阶。"}
            for index in range(4)
        ],
        "expressions": [
            {"id": "river-source", "text": "河水涨过台阶。", "role": "彼此正在承担的过往"},
            {"id": "stone-source", "text": "石头有了裂缝。", "role": "另一种来处"},
        ],
    }
    draft["positions"][0]["seed"] = ("# 私人生活\n\n" + "她记得涨水的夏日。" * 2000)[:12000]
    status, created = server.request("POST", "/api/presets", draft)
    check(status == 200 and created["preset"]["origin"] == "custom"
          and created["preset"]["editable"] is True, "HTTP 保存自定义预设与长 Markdown")
    original = created["preset"]
    status, catalog = server.request("GET", "/api/presets")
    check(status == 200 and original in catalog["presets"] and not catalog["errors"], "目录读取完整自定义资料")
    _, empty = server.request("GET", "/api/runs")
    existing_ids = {run["id"] for run in empty["runs"]}

    directory = Path(original["storage_path"])
    expected_root = (server.directory / "data" / "presets").resolve()
    check(directory.is_relative_to(expected_root), "自定义文件只写入隔离的私人预设目录")
    manifest = json.loads((directory / "preset.json").read_text(encoding="utf-8"))
    materials_match = manifest["positions"] == [
        {"id": position["id"], "name": position["name"]} for position in draft["positions"]
    ]
    for position in draft["positions"]:
        character = directory / "characters" / position["id"]
        materials_match &= (character / "character.md").read_text(encoding="utf-8") == position["seed"]
        materials_match &= json.loads((character / "character.json").read_text(encoding="utf-8")) == {
            "id": position["id"], "name": position["name"],
        }
    check(materials_match, "人物 MD 与仅 ID/name 的 JSON 分别落盘")
    check(all(
        stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        for path in [expected_root, *expected_root.rglob("*")]
    ), "隔离预设目录 700 与明文文件 600 权限")
    _, after_save = server.request("GET", "/api/runs")
    check({run["id"] for run in after_save["runs"]} == existing_ids,
          "保存预设不创建或启动模型实验")

    status, custom = server.request("POST", "/api/runs", {
        "preset_id": draft["id"], "provider": "fixture",
        "limits": {"max_calls": 80, "max_tokens": 40000, "max_parallel": 4, "max_windows": 6},
    })
    check(status == 200 and custom["run"]["usage"]["calls"] == 0
          and len(custom["positions"]) == 4, "自定义预设创建实验时不调用模型")
    custom_id = id_of(custom)
    edited = json.loads(json.dumps(draft))
    edited["name"] = "河边的第二稿"
    edited["positions"][0]["seed"] = "# 新的过往\n\n她记得另一场夏日的雨。"
    edited["expressions"][0]["text"] = "河水已经退到台阶下。"
    status, updated = server.request("PUT", f"/api/presets/{draft['id']}", {
        **edited, "expected_version": original["version"],
    })
    check(status == 200 and updated["preset"]["version"] != original["version"],
          "带当前版本的预设编辑发布完整新版本")
    current = updated["preset"]
    _, untouched = server.request("GET", f"/api/runs/{custom_id}")
    check(untouched == custom, "编辑预设不回写已创建实验")
    status, conflict = server.request("PUT", f"/api/presets/{draft['id']}", {
        **draft, "expected_version": original["version"],
    })
    check(status == 409 and conflict["error"]["code"] == "PRESET_CONFLICT", "过时编辑版本安全拒绝")
    status, conflict = server.request("POST", "/api/presets", draft)
    check(status == 409 and conflict["error"]["code"] == "PRESET_CONFLICT", "新建重复预设标识安全拒绝")
    for method, path, body in [
        ("POST", "/api/presets", {**draft, "id": "letters"}),
        ("PUT", "/api/presets/letters", {**draft, "id": "letters", "expected_version": original["version"]}),
    ]:
        status, readonly = server.request(method, path, body)
        check(status == 409 and readonly["error"]["code"] == "PRESET_READ_ONLY", "内置预设不可覆盖")
    _, catalog = server.request("GET", "/api/presets")
    check(current in catalog["presets"] and (directory / "characters" / "person_0" / "character.md").read_text(
        encoding="utf-8"
    ) == draft["positions"][0]["seed"], "冲突后当前资料与历史版本保持完整")
    pointer = directory.parent.parent / "current.json"
    check(json.loads(pointer.read_text(encoding="utf-8"))["version"] == current["version"],
          "当前指针指向已完整保存的新版本")
    status, new_run = server.request("POST", "/api/runs", {"preset_id": draft["id"], "provider": "fixture"})
    check(status == 200 and new_run["positions"][0]["seed"] == edited["positions"][0]["seed"],
          "后续新实验采用修改后的资料")

    for revision in (1, 2):
        status, custom_advanced = server.request("POST", f"/api/runs/{custom_id}/step")
        check(status == 200 and custom_advanced["run"]["revision"] == revision,
              "任意共享表达 ID 连续执行自定义窗口")
    targets = custom_advanced["audit"]["joint_targets"]
    check(len(targets) == 4 and all(len(target["sources"]) == 3 for target in targets)
          and all(target["expression_id"] == "river-source" for target in targets),
          "自定义用法接受全部同伴的共同输入")
    check(all(set(segment["expression_ids"]) == {"river-source", "stone-source"}
              and "那封信" not in segment["text"] for segment in custom_advanced["story"]),
          "呈现承接自定义共享载体且不强制变成书信")
    status, counterfactual = server.request("POST", f"/api/runs/{custom_id}/counterfactual", {
        "target_position_id": "person_0", "source_position_id": "person_1",
    })
    check(status == 200 and counterfactual["structural_effect"] is True
          and counterfactual["semantic_fixture_effect"] is True, "自定义载体的反事实回流有效")
    _, unchanged = server.request("GET", f"/api/runs/{custom_id}")
    check(unchanged == custom_advanced, "自定义消融不修改原档")
    status, replay = server.request("GET", f"/api/runs/{custom_id}/replay")
    check(status == 200 and replay.get("ok") is True, "自定义预设的语义事件可重新执行")
    status, fork = server.request("POST", f"/api/runs/{custom_id}/fork", {"title": "自定义验收分支"})
    check(status == 200 and id_of(fork) != custom_id, "自定义存档可建立独立分支")
    status, fork_advanced = server.request("POST", f"/api/runs/{id_of(fork)}/step")
    _, unchanged = server.request("GET", f"/api/runs/{custom_id}")
    check(status == 200 and fork_advanced["run"]["revision"] == 3 and unchanged == custom_advanced,
          "自定义分支继续不修改原档")

    server.stop()
    server.start()
    status, catalog = server.request("GET", "/api/presets")
    check(status == 200 and current in catalog["presets"] and not catalog["errors"],
          "服务重启后自定义目录及 Markdown 完整恢复")
    status, restored = server.request("GET", f"/api/runs/{custom_id}")
    check(status == 200 and restored == custom_advanced, "重启后旧资料实验仍保持原运行状态")
    status, restored_replay = server.request("GET", f"/api/runs/{custom_id}/replay")
    check(status == 200 and restored_replay.get("ok") is True
          and restored_replay.get("digest") == replay.get("digest"), "重启后自定义事件重放摘要稳定")
    return [fork_advanced, new_run, custom_advanced]


def verify(server: Server) -> None:
    status, health = server.request("GET", "/api/health")
    check(status == 200 and health.get("ok") is True, "真实 HTTP 健康检查")
    status, config = server.request("GET", "/api/config")
    check(status == 200 and config.get("configured") is False, "隔离配置不加载用户密钥")
    expected_bounds = {"max_calls": 1000, "max_tokens": 512000, "max_parallel": 8, "max_windows": 1000}
    check(config.get("budget_bounds") == expected_bounds, "公开配置声明统一的 512K 与调度预算上限")
    assert_safe_projection(config)
    status, verification = server.request("GET", "/api/verify")
    check(status == 200 and verification.get("ok") is True and bool(verification.get("checks")) and all(
        item.get("passed") is True for item in verification.get("checks", [])
    ), "内置离线机制验收")
    status, rejected = server.request("POST", "/api/runs", {
        "preset_id": "letters", "provider": "fixture", "limits": {"max_tokens": 512001},
    })
    check(status == 422 and rejected["error"]["code"] == "REQUEST_INVALID",
          "512001 Token 预算在创建前安全拒绝")
    status, run = server.request("POST", "/api/runs", {
        "preset_id": "letters", "provider": "fixture", "limits": {"max_tokens": 512000},
    })
    check(status == 200 and run["run"]["revision"] == 0 and run["run"]["limits"]["max_tokens"] == 512000,
          "创建并保存 512000 Token 的 fixture 对局")
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
    raised_limits = {"max_calls": 14, "max_tokens": 512000, "max_parallel": 1, "max_windows": 2}
    status, replenished = server.request("PATCH", f"/api/runs/{id_of(limited)}/limits", raised_limits)
    check(status == 200 and replenished["run"]["status"] == "paused"
          and replenished["run"]["last_error"] is None
          and replenished["run"]["limits"] == raised_limits
          and replenished["run"]["usage"] == exhausted["run"]["usage"]
          and replenished["pending"] == exhausted["pending"]
          and replenished["run"]["revision"] == exhausted["run"]["revision"],
          "耗尽后保存预算保留恢复点与用量且不发起生成")
    status, resumed = server.request("POST", f"/api/runs/{id_of(limited)}/step")
    check(status == 200 and resumed["run"]["revision"] == 1 and resumed["run"]["usage"]["windows"] == 1,
          "预算补充后原存档继续完成窗口")
    status, rejected = server.request("PATCH", f"/api/runs/{id_of(limited)}/limits", {
        **raised_limits, "max_calls": 1,
    })
    _, after_rejection = server.request("GET", f"/api/runs/{id_of(limited)}")
    check(status == 422 and rejected["error"]["code"] == "BUDGET_BELOW_USAGE" and after_rejection == resumed,
          "已有调用不能被降额抹除且拒绝后存档不变")
    status, resumed_replay = server.request("GET", f"/api/runs/{id_of(limited)}/replay")
    check(status == 200 and resumed_replay.get("ok") is True,
          "预算保存与恢复事件不破坏语义重放")
    status, background = server.request("POST", "/api/runs", {"preset_id": "letters", "provider": "fixture"})
    check(status == 200 and background["run"]["limits"]["max_tokens"] == 128000,
          "创建后台控制对局采用 128000 Token 默认预算")
    server.request("POST", f"/api/runs/{id_of(background)}/start")
    server.request("POST", f"/api/runs/{id_of(background)}/pause")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, paused = server.request("GET", f"/api/runs/{id_of(background)}")
        if paused["run"]["status"] != "running":
            break
        time.sleep(0.05)
    check(paused["run"]["status"] != "running", "后台开始与协作式暂停")
    custom_runs = verify_custom_presets(server)
    for snapshot in [background, limited, fork, advanced, *custom_runs]:
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
