"""Verify executable reflexive methods on an isolated fixture-only HTTP server."""

from __future__ import annotations

import tempfile
from pathlib import Path

from verify_demo import Server, VerificationFailure, assert_safe_projection, check, id_of, reading_of


def require(server: Server, method: str, path: str, body: dict | None = None) -> dict:
    status, value = server.request(method, path, body)
    check(status == 200, f"HTTP {method} {path.rsplit('/', 1)[-1]} 完成")
    return value


def validate_records(records: dict, positions: set[str]) -> None:
    methods, choices, adoptions = records["methods"], records["choices"], records["adoptions"]
    method_ids = {method["id"] for method in methods}
    choice_ids = {choice["id"] for choice in choices}
    check(bool(methods) and bool(choices), "方法与实际选择均形成持久记录")
    check(len(method_ids) == len(methods) and len(choice_ids) == len(choices), "方法与选择具有唯一接口")
    check(any(set(method["inputs"]) & method_ids for method in methods), "方法采用已有方法作为输入")
    check(any(set(method["inputs"]) & choice_ids for method in methods), "方法采用实际选择作为输入")
    check(any(method.get("revises") in method_ids for method in methods), "方法修订保留既有版本谱系")
    check(all(set(active) <= method_ids for active in adoptions.values()), "作用域采用仅指向已保存方法")
    check(all(set(choice["methods"]) <= method_ids for choice in choices), "实际选择保留已执行方法依赖")
    check(all(choice["input_fingerprint"] and choice["materials"] for choice in choices),
          "实际选择保存指纹及被采用的材料快照")
    check({choice["stage"] for choice in choices} == {"propose", "realize", "present"},
          "提案、续写及阅读均记录真实输入选择")
    check(any(choice["methods"] and choice["groups"] for choice in choices), "已采用方法确实进入执行输入")
    by_id = {method["id"]: method for method in methods}

    def depth(method_id: str, path: frozenset[str] = frozenset()) -> int:
        if method_id in path:
            raise VerificationFailure("已保存方法依赖存在循环")
        return 1 + max((depth(ref, path | {method_id}) for ref in by_id[method_id]["inputs"]
                        if ref in by_id), default=0)

    check(max(depth(method_id) for method_id in by_id) >= 3, "方法引用链具有至少三层可执行组合")
    reading_methods = {method["id"] for method in methods if method["source_scope"].startswith("reading:")}
    check(bool(reading_methods) and any(
        set(active) & reading_methods for scope, active in adoptions.items()
        if scope in positions or scope.removeprefix("position:") in positions
    ), "阅读产生的组织方法返回人物作用域")
    check(all(
        by_id[input_id]["created_revision"] <= method["created_revision"]
        for method in methods for input_id in method["inputs"] if input_id in method_ids
    ), "方法引用来自已存在版本，不依赖同批尚未生成的草稿")
    assert_safe_projection(records)


def verify(server: Server) -> None:
    config = require(server, "GET", "/api/config")
    check(config["configured"] is False, "隔离服务未加载用户密钥")
    classic = require(server, "POST", "/api/runs", {
        "preset_id": "letters", "provider": "fixture",
        "limits": {"max_calls": 100, "max_tokens": 5120000, "max_parallel": 3, "max_windows": 8},
    })
    check(classic["run"]["mode"] == "classic" and classic["schema_version"] == 2
          and classic.get("reflection") is None, "省略模式仍创建经典对照实验")
    classic_path = f"/api/runs/{id_of(classic)}"
    classic = require(server, "POST", classic_path + "/step")
    check(classic["run"]["revision"] == 1 and bool(classic["audit"]["joint_targets"]),
          "经典模式继续采用原有共同承接机制")

    created = require(server, "POST", "/api/runs", {
        "preset_id": "letters", "provider": "fixture", "mode": "reflexive",
        "limits": {"max_calls": 100, "max_tokens": 5120000, "max_parallel": 3, "max_windows": 8},
    })
    check(created["schema_version"] == 3 and created["run"]["mode"] == "reflexive"
          and created["run"]["usage"]["calls"] == 0 and created["reflection"]["methods"] == [],
          "显式创建反身 schema3 实验且创建不调用生成器")
    path = f"/api/runs/{id_of(created)}"
    added = require(server, "POST", path + "/readings", {
        "title": "隔离的第二阅读", "start_position_id": "reader", "enabled": True,
    })
    other = next(track for track in added["reading_tracks"] if track["id"] != "main")
    check(added["run"]["usage"]["calls"] == 0, "第二阅读的配置保存不调用模型")
    first_methods = None
    for revision in range(1, 5):
        advanced = require(server, "POST", path + "/step")
        check(advanced["run"]["revision"] == revision and advanced["pending"] is None
              and advanced["run"]["last_error"] is None, f"反身窗口 {revision} 完整结束")
        records = require(server, "GET", path + "/reflection")
        check(records["mode"] == "reflexive", "反身诊断接口声明模式")
        if first_methods is None:
            first_methods = {method["id"]: method for method in records["methods"]}
        else:
            check(all(method in records["methods"] for method in first_methods.values()),
                  "方法后续修订保留最初不可变版本")
        replay = require(server, "GET", path + "/replay")
        check(replay["ok"] and all(item["passed"] for item in replay["checks"]),
              f"窗口 {revision} 从独立事件证据重放一致")
    validate_records(records, {position["id"] for position in advanced["positions"]})
    check(len(advanced["story"]) == len(reading_of(advanced, other["id"])["story"]) == 4,
          "两条阅读共同跟随四个世界窗口且各自保存历史")
    check(reading_of(advanced, other["id"])["story"][0]["focus_position_id"] == "reader",
          "第二阅读保持自己的起始位置约束")
    assert_safe_projection(advanced)

    before = require(server, "GET", path)
    reports = [
        require(server, "POST", path + "/reflection/ablate", {"scope_id": scope, "method_id": method_id})
        for scope, active in records["adoptions"].items() for method_id in active
    ]
    check(bool(reports) and any(report["structural_effect"] for report in reports),
          "移除至少一个采用的方法改变实际选择或输入组织")
    check(any(report["before_selected"] != report["after_selected"] for report in reports),
          "消融至少改变一项材料选择，而不只是更换方法编号")
    check(all(report["before_fingerprint"] and report["after_fingerprint"] for report in reports),
          "方法消融返回编译输入的前后指纹")
    check(require(server, "GET", path) == before, "全部 detached 消融不修改原实验或调用用量")

    fork = require(server, "POST", path + "/fork", {"title": "隔离反身分支"})
    fork_path = f"/api/runs/{id_of(fork)}"
    fork = require(server, "POST", fork_path + "/step")
    check(fork["run"]["revision"] == 5 and fork["run"]["mode"] == "reflexive"
          and require(server, "GET", path) == before, "反身存档分支继续不改变原实验")
    check(require(server, "GET", fork_path + "/replay")["ok"], "分支继承的反身证据可执行重放")

    server.stop()
    server.start()
    check(require(server, "GET", path) == before, "进程重启保持反身方法、采用、选择及双阅读")
    restored_replay = require(server, "GET", path + "/replay")
    check(restored_replay["ok"] and restored_replay["digest"] == replay["digest"],
          "重启后的独立语义重放摘要稳定")
    for run_path in (classic_path, path, fork_path):
        check(require(server, "DELETE", run_path)["deleted"] is True, "删除隔离实验及其事件证据")
    check(require(server, "GET", "/api/runs")["runs"] == [], "隔离数据库最终为空")


def main() -> None:
    print("开始隔离反身 HTTP 验收：仅 fixture；不读取用户配置，不调用付费模型。")
    with tempfile.TemporaryDirectory(prefix="opentale-reflexive-") as directory:
        server = None
        try:
            server = Server(Path(directory))
            server.start()
            verify(server)
        except VerificationFailure as error:
            print(f"FAIL：{error}")
            raise SystemExit(1) from None
        except (KeyError, ValueError, OSError):
            print("FAIL：反身验收未通过；仅输出安全标签，不显示服务原始日志。")
            raise SystemExit(1) from None
        finally:
            if server:
                server.stop()
    print("反身执行、消融和重放通过；夹具不证明真实模型自发意图或文学效果。")


if __name__ == "__main__":
    main()
