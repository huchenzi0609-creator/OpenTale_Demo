"""Recoverable async evaluator of expression programs and reading continuations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import uuid4

from .config import Settings
from .domain import Event, Pending, Presentation, ReadingFrame, Snapshot
from .kernel import KernelError, audit_for, frame_for, install_presentation, validate_proposal, without_source
from .kernel import install_batch as install
from .providers import ModelResult, ProviderError, make_provider
from .storage import Store

T = TypeVar("T")


class Runner:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.pause_requests: set[str] = set()

    def busy(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        return bool((task and not task.done()) or (run_id in self.locks and self.locks[run_id].locked()))

    def event(self, state: Snapshot, kind: str, **data: Any) -> None:
        state.events.append(Event(id=uuid4().hex, type=kind, revision=state.run.revision, data=data))
        self.store.save(state)

    def pause(self, run_id: str) -> Snapshot:
        self.pause_requests.add(run_id)
        state = self.store.load(run_id)
        # Let the in-flight phase save its results; this handler must not overwrite their snapshot.
        if not self.busy(run_id):
            state.run.status = "paused"
            self.event(state, "paused", boundary="idle")
        return state

    async def start(self, run_id: str) -> Snapshot:
        if self.busy(run_id):
            raise KernelError("RUN_BUSY", "该实验已有生成任务正在执行。")
        self.store.load(run_id)
        self.pause_requests.discard(run_id)
        self.tasks[run_id] = asyncio.create_task(self._loop(run_id))
        # Mark running before responding so polling cannot mistake the accepted job for idle.
        state = self.store.load(run_id)
        state.run.status = "running"
        self.store.save(state)
        return state

    async def _loop(self, run_id: str) -> None:
        while run_id not in self.pause_requests:
            try:
                state = await self.step(run_id, automatic=True)
            except (KernelError, ProviderError):
                break
            if state.run.status != "running":
                break
            await asyncio.sleep(0.35)
        state = self.store.load(run_id)
        if state.run.status == "running":
            state.run.status = "paused"
            self.event(state, "paused", boundary=state.run.phase)

    async def shutdown(self) -> None:
        self.pause_requests.update(self.tasks)
        live = [task for task in self.tasks.values() if not task.done()]
        for task in live:
            task.cancel()
        await asyncio.gather(*live, return_exceptions=True)

    def _needed_calls(self, state: Snapshot) -> int:
        pending = state.pending
        if not pending:
            return len(state.positions) * 2 + 1
        drafts = 0 if pending.committed else len(state.positions) - len(pending.proposals)
        return drafts + len(state.positions) - len(pending.realizations) + 1

    async def _call(
        self, state: Snapshot, frame: Any, call: Callable[[], Awaitable[ModelResult[T]]]
    ) -> ModelResult[T]:
        usage, limits = state.run.usage, state.run.limits
        estimate = len(frame.model_dump_json().encode("utf-8")) + 4096 + self.settings.max_output_tokens
        if state.run.provider == "fixture":
            estimate = 0  # Deterministic local execution makes no token-billed call.
        used = usage.input_tokens + usage.output_tokens + usage.unknown_tokens + usage.reserved_tokens
        if usage.calls >= limits.max_calls or used + estimate > limits.max_tokens:
            raise KernelError("BUDGET_EXHAUSTED", "预算不足以安全发起下一次调用；已完成的阶段已保存。")
        usage.calls += 1
        usage.reserved_tokens += estimate
        self.store.save(state)
        try:
            result = await call()
        except BaseException:
            usage.reserved_tokens -= estimate
            usage.unknown_tokens += estimate
            usage.failed_calls += 1
            self.store.save(state)
            raise
        usage.reserved_tokens -= estimate
        usage.input_tokens += result.input_tokens
        usage.output_tokens += result.output_tokens
        # Caller immediately persists accepted result + settled usage in one checkpoint.
        return result

    async def step(self, run_id: str, automatic: bool = False) -> Snapshot:
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise KernelError("RUN_BUSY", "该实验已有生成任务正在执行。")
        async with lock:
            state = self.store.load(run_id)
            if not automatic:
                task = self.tasks.get(run_id)
                if task and not task.done():
                    raise KernelError("RUN_BUSY", "请先暂停自动生成，再执行单步。")
                self.pause_requests.discard(run_id)
            state.run.status = "running"
            state.run.last_error = None
            try:
                provider = make_provider(state.run.provider, self.settings)
                if state.run.usage.windows >= state.run.limits.max_windows:
                    raise KernelError("BUDGET_EXHAUSTED", "本实验的局部窗口额度已用完，可建立分支继续。")
                if state.run.usage.calls + self._needed_calls(state) > state.run.limits.max_calls:
                    raise KernelError("BUDGET_EXHAUSTED", "剩余调用额度不足以完成一个窗口；没有发起新调用。")
                if state.pending is None:
                    state.pending = Pending(base_revision=state.run.revision)
                    state.run.phase = "drafting"
                    self.event(state, "window_started", base_revision=state.run.revision)
                pending = state.pending
                semaphore = asyncio.Semaphore(state.run.limits.max_parallel)

                async def draft(position_id: str) -> None:
                    if position_id in pending.proposals:
                        return
                    async with semaphore:
                        frame = frame_for(state, position_id)
                        result = await self._call(state, frame, lambda: provider.propose(frame))
                        if result.value.position_id != position_id:
                            raise KernelError("MODEL_OUTPUT_INVALID", "模型返回了不匹配的生成位置。")
                        validate_proposal(state, result.value)
                        pending.proposals[position_id] = result.value
                        self.event(
                            state,
                            "proposal_ready",
                            position_id=position_id,
                            based_on_revision=result.value.based_on_revision,
                        )

                if not pending.committed:
                    results = await asyncio.gather(
                        *(draft(p.id) for p in state.positions), return_exceptions=True
                    )
                    failure = next((r for r in results if isinstance(r, BaseException)), None)
                    if failure:
                        raise failure
                    try:
                        state = install(state, list(pending.proposals.values()), pending.base_revision)
                    except KernelError:
                        # Invalid collective drafts are not cached forever across retry.
                        state.pending = Pending(base_revision=state.run.revision)
                        self.store.save(state)
                        raise
                    pending = state.pending
                    assert pending is not None
                    pending.committed = True
                    state.run.phase = "realizing"
                    self.event(
                        state,
                        "batch_installed",
                        changes=state.audit["condition_changes"],
                        joint_targets=state.audit["joint_targets"],
                        base_revision=pending.base_revision,
                        proposals=[p.model_dump() for p in pending.proposals.values()],
                    )
                if run_id in self.pause_requests:
                    state.run.status = "paused"
                    self.event(state, "paused", boundary="after_install")
                    return state

                async def realize(position_id: str) -> None:
                    if position_id in pending.realizations:
                        return
                    async with semaphore:
                        frame = frame_for(state, position_id)
                        result = await self._call(state, frame, lambda: provider.realize(frame))
                        if not result.value.strip():
                            raise KernelError("MODEL_OUTPUT_INVALID", "恢复生成没有返回可读内容。")
                        pending.realizations[position_id] = result.value
                        continuation = next(c for c in state.continuations if c.position_id == position_id)
                        continuation.surface = result.value
                        continuation.status = "ready"
                        continuation.resume_count += 1
                        self.event(
                            state,
                            "continuation_resumed",
                            position_id=position_id,
                            frame_revision=frame.revision,
                            condition_fingerprint=frame.condition_fingerprint,
                            surface=result.value,
                        )

                results = await asyncio.gather(
                    *(realize(p.id) for p in state.positions), return_exceptions=True
                )
                failure = next((r for r in results if isinstance(r, BaseException)), None)
                if failure:
                    raise failure
                state.run.phase = "presenting"
                self.store.save(state)
                if run_id in self.pause_requests:
                    state.run.status = "paused"
                    self.event(state, "paused", boundary="before_presentation")
                    return state
                reading_frame = self.reading_frame(state)
                result = await self._call(state, reading_frame, lambda: provider.present(reading_frame))
                self.apply_presentation(state, result.value)
                state.pending = None
                state.run.usage.windows += 1
                state.run.phase = "idle"
                state.run.status = (
                    "paused" if run_id in self.pause_requests else "running" if automatic else "idle"
                )
                self.event(
                    state,
                    "presented",
                    segment_id=state.story[-1].id,
                    reinterprets=state.story[-1].reinterprets,
                    presentation=result.value.model_dump(),
                )
                if state.run.status == "paused":
                    self.event(state, "paused", boundary="after_presentation")
                return state
            except asyncio.CancelledError:
                state.run.status = "paused"
                self.event(state, "paused", boundary=state.run.phase)
                raise
            except (KernelError, ProviderError) as exc:
                state.run.status = "budget_exhausted" if exc.code == "BUDGET_EXHAUSTED" else "error"
                state.run.last_error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
                self.event(
                    state,
                    "budget_exhausted" if exc.code == "BUDGET_EXHAUSTED" else "error",
                    code=exc.code,
                    message=exc.message,
                )
                raise
            except Exception:
                state.run.status = "error"
                state.run.last_error = {
                    "code": "RUNTIME_FAILURE",
                    "message": "运行阶段未能完成；可从已保存阶段重试。",
                    "retryable": True,
                }
                self.event(state, "error", **state.run.last_error)
                raise ProviderError(
                    "RUNTIME_FAILURE", state.run.last_error["message"], retryable=True
                ) from None

    def reading_frame(self, state: Snapshot) -> ReadingFrame:
        shared_ids = {entry["expression_id"] for entry in audit_for(state)["shared_expressions"]}
        return ReadingFrame(
            revision=state.run.revision,
            candidates=[
                {
                    "position_id": p.id,
                    "name": p.name,
                    "text": next(c.surface for c in state.continuations if c.position_id == p.id),
                    "active_uses": [
                        use
                        for use in frame_for(state, p.id).active_uses
                        if use["expression_id"] in shared_ids
                    ],
                }
                for p in state.positions
            ],
            previous_segments=[s.model_dump() for s in state.story[-3:]],
            open_relations=state.reading.open_relations,
            current_focus=state.reading.focus_position_id,
        )

    def apply_presentation(self, state: Snapshot, presentation: Presentation) -> None:
        install_presentation(state, presentation)

    async def counterfactual(self, run_id: str, target_id: str, source_id: str) -> dict[str, Any]:
        state = self.store.load(run_id)
        positions = {p.id for p in state.positions}
        if target_id not in positions or source_id not in positions or target_id == source_id:
            raise KernelError("COUNTERFACTUAL_TARGET_INVALID", "请选择两个不同的现有位置。")
        changed_state, ids = without_source(state, target_id, source_id)
        baseline, changed = frame_for(state, target_id), frame_for(changed_state, target_id)
        fixture = make_provider("fixture", self.settings)
        before, after = await fixture.realize(baseline), await fixture.realize(changed)
        return {
            "baseline_fingerprint": baseline.condition_fingerprint,
            "changed_fingerprint": changed.condition_fingerprint,
            "changed_use_ids": ids,
            "baseline_text": before.value,
            "changed_text": after.value,
            "structural_effect": baseline.condition_fingerprint != changed.condition_fingerprint,
            "semantic_fixture_effect": before.value != after.value,
            "note": "只在脱离原存档的副本上使用离线演示求值器；证明条件回流，不证明真实模型语义能力。",
        }
