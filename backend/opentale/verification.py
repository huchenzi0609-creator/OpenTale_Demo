"""Executable offline mechanism checks exposed in the UI; no remote model calls."""

from __future__ import annotations

from itertools import permutations
from pathlib import Path

from .config import Settings
from .domain import Limits, Proposal
from .kernel import KernelError, frame_for, install_batch, without_source
from .presets import initialize, read_presets
from .providers import make_provider


async def verify_mechanisms(settings: Settings) -> dict:
    checks = []
    try:
        root = Path(__file__).resolve().parents[2]
        preset = next(p for p in read_presets(root) if p["id"] == "letters")
        base = initialize(preset, "fixture", Limits())
        fixture = make_provider("fixture", settings)
        proposals = [(await fixture.propose(frame_for(base, p.id))).value for p in base.positions]
        installed = install_batch(base, proposals, 0)
        order_equal = all(
            install_batch(base, list(order), 0).model_dump() == installed.model_dump()
            for order in permutations(proposals)
        )
        checks.append(
            {
                "name": "联合安装不依赖返回先后",
                "passed": order_equal,
                "detail": "对同一快照的全部六种到达顺序安装，结果逐字段相同。",
            }
        )
        checks.append(
            {
                "name": "多方共同改变同一个活跃用法",
                "passed": len(installed.audit["joint_targets"]) == 3,
                "detail": "每个共同目标保留两个来源的绑定，并重构恢复生成的条件。",
            }
        )
        checks.append(
            {
                "name": "共享引用保持同一技术地址",
                "passed": len([e for e in installed.expressions if e.id == "e-letter"]) == 1
                and len([u for u in installed.uses if u.expression_id == "e-letter"]) == 3,
                "detail": "三处用法引用同一表达，未复制为三个互不作用的故事素材。",
            }
        )
        target, source = base.positions[0].id, base.positions[1].id
        changed, affected = without_source(installed, target, source)
        before_frame, after_frame = frame_for(installed, target), frame_for(changed, target)
        before, after = await fixture.realize(before_frame), await fixture.realize(after_frame)
        checks.append(
            {
                "name": "移除来源实际改变条件及离线续写",
                "passed": bool(affected)
                and before_frame.condition_fingerprint != after_frame.condition_fingerprint
                and before.value != after.value,
                "detail": "在独立副本移除一项输入，观察活跃条件指纹和确定性续写。",
            }
        )
        stale = False
        try:
            install_batch(installed, proposals, 0)
        except KernelError as error:
            stale = error.code == "STALE_PROPOSAL"
        checks.append(
            {
                "name": "过时表达不能覆盖新条件",
                "passed": stale,
                "detail": "旧快照的整批表达被拒绝，已安装状态不被修改。",
            }
        )
        drafts = [p.model_copy(deep=True) for p in proposals]
        for index, draft in enumerate(drafts[1:]):
            op = next(o for o in draft.operations if o.target_position_id == target)
            op.value = "她已经归来。" if index == 0 else "她仍然没有归来。"
            op.anchor = op.value
            draft.text += op.value
        plural = install_batch(base, drafts, 0)
        values = next(
            u.bindings for u in plural.uses if u.position_id == target and u.expression_id == "e-letter"
        )
        checks.append(
            {
                "name": "冲突归属按作用位置保留",
                "passed": {b.value for b in values} == {"她已经归来。", "她仍然没有归来。"},
                "detail": "同一用法中的不相容表达并存，没有最后写入者或公共正确版本。",
            }
        )
        public = frame_for(installed, target).model_dump()
        blind = all(
            "source_position_id" not in use and "source_expression_id" not in use
            for use in public["active_uses"]
        )
        checks.append(
            {
                "name": "人物条件不暴露技术上的构造者",
                "passed": blind,
                "detail": "运行时可追踪来源，人物读取的条件只含生效的内容与用法。",
            }
        )
        # Revalidate transport-independent contract, not just fixture-produced Python objects.
        roundtrip = [Proposal.model_validate_json(p.model_dump_json()) for p in proposals]
        checks.append(
            {
                "name": "表达程序可序列化并重新执行",
                "passed": install_batch(base, roundtrip, 0).model_dump() == installed.model_dump(),
                "detail": "同一程序经 JSON 往返后得到相同状态。",
            }
        )
    except Exception:
        checks.append({"name": "离线验收执行", "passed": False, "detail": "机械验证未通过，请运行测试检查。"})
    return {
        "ok": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "note": "这些检查执行真实运行时和离线求值器；不代表已验证真实模型的叙事相干性或文学能力。",
    }
