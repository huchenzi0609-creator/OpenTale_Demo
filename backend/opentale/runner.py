"""Recoverable async evaluator of expression programs and reading continuations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import uuid4

from .capacity import MAX_FORMAT_REPAIRS, MAX_READING_TRACKS
from .config import Settings
from .domain import Event, Limits, Pending, Presentation, ReadingFrame, ReadingTrack, Snapshot
from .kernel import (
    KernelError,
    fingerprint,
    frame_for,
    install_presentation,
    reading_frame_for,
    reading_track_for,
    validate_proposal,
    without_source,
)
from .kernel import install_batch as install
from .providers import ModelResult, ProviderError, make_provider
from .reflection import capture_choice, install_feedback, position_scope, reading_scope
from .reflection_types import ReflectionFrame
from .request_evidence import execution_signature, existing_request, freeze_request, request_chars
from .request_types import RequestAttempt, RequestEnvelope
from .scheduler import begin_plan, observe_job
from .storage import Store

T = TypeVar("T")


class Runner:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.pause_requests: set[str] = set()
        self._funded_jobs: dict[str, set[str]] = {}

    def busy(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        return bool((task and not task.done()) or (run_id in self.locks and self.locks[run_id].locked()))

    def any_busy(self) -> bool:
        return any(self.busy(run_id) for run_id in self.tasks.keys() | self.locks.keys())

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

    def update_limits(self, run_id: str, limits: Limits) -> Snapshot:
        """Change admission bounds at a stable checkpoint without resetting paid work."""
        state = self.store.load(run_id)
        if self.busy(run_id) or state.run.status == "running":
            raise KernelError("RUN_BUSY", "请先暂停本实验，再更新预算。")
        usage = state.run.usage
        occupied = usage.input_tokens + usage.output_tokens + usage.unknown_tokens + usage.reserved_tokens
        if any(limit is not None and limit < used for limit, used in (
            (limits.max_calls, usage.calls), (limits.max_tokens, occupied),
            (limits.max_windows, usage.windows),
        )):
            raise KernelError("BUDGET_BELOW_USAGE", "新预算不能小于本实验已用调用、Token 占用或已完成窗口。")
        previous = state.run.limits.model_dump()
        state.run.limits = limits.model_copy(deep=True)
        if state.run.status == "budget_exhausted":
            state.run.status = "paused"
            if state.run.last_error and state.run.last_error.get("code") == "BUDGET_EXHAUSTED":
                state.run.last_error = None
        self.event(state, "limits_updated", previous=previous, limits=state.run.limits.model_dump())
        return state

    def _stable(self, run_id: str) -> Snapshot:
        state = self.store.load(run_id)
        if self.busy(run_id) or state.run.status == "running":
            raise KernelError("RUN_BUSY", "请先暂停生成，再配置或单独生成连续阅读。")
        return state

    def release_method(self, run_id: str, scope_id: str, method_id: str) -> Snapshot:
        state = self._stable(run_id)
        if state.reflection is None or method_id not in state.reflection.adoptions.get(scope_id, []):
            raise KernelError("METHOD_REFERENCE_INVALID", "请选择该作用范围已经采用的方法。")
        if state.run.mode == "reflexive_v2" and state.pending is not None:
            release = {"scope_id": scope_id, "method_id": method_id}
            if release not in state.pending.deferred_releases:
                state.pending.deferred_releases.append(release)
                self.event(state, "method_release_queued", **release)
            return state
        state.reflection.adoptions[scope_id].remove(method_id)
        if state.run.mode == "reflexive_v2":
            state.policy_revision += 1
        self.event(state, "method_released", scope_id=scope_id, method_id=method_id)
        return state

    def add_reading(self, run_id: str, title: str, start_position_id: str, enabled: bool = True) -> Snapshot:
        state = self._stable(run_id)
        if start_position_id not in {position.id for position in state.positions}:
            raise KernelError("READING_START_INVALID", "请选择本世界中的起始人物。")
        if not title.strip() or len(title.strip()) > 120:
            raise KernelError("READING_TITLE_INVALID", "请填写不超过 120 字符的阅读名称。")
        if len(state.reading_tracks) >= MAX_READING_TRACKS:
            raise KernelError("READING_LIMIT_REACHED", "本实验的连续阅读数量已达到上限。")
        track = ReadingTrack(
            id=uuid4().hex, title=title.strip(), start_position_id=start_position_id,
            created_revision=state.run.revision, enabled=enabled,
        )
        state.reading_tracks.append(track)
        self.event(state, "reading_created", track=track.model_dump())
        return state

    def set_reading_enabled(self, run_id: str, reading_id: str, enabled: bool) -> Snapshot:
        state = self._stable(run_id)
        track = reading_track_for(state, reading_id)
        if track.enabled != enabled:
            track.enabled = enabled
            self.event(state, "reading_enabled", reading_id=reading_id, enabled=enabled)
        return state

    @staticmethod
    def _presented_revision(track: ReadingTrack, revision: int) -> bool:
        return any(segment.revision == revision for segment in track.story)

    @staticmethod
    def _world_ready(state: Snapshot) -> bool:
        return state.run.revision >= 1 and all(
            continuation.frame_revision == state.run.revision
            and continuation.status == "ready" and bool(continuation.surface.strip())
            for continuation in state.continuations
        ) and len(state.continuations) == len(state.positions) and (
            state.pending is None or not state.pending.committed
            or set(state.pending.realizations) == {position.id for position in state.positions}
        )

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
            return len(state.positions) * 2 + sum(track.enabled for track in state.reading_tracks)
        drafts = 0 if pending.committed else len(state.positions) - len(pending.proposals)
        readers = sum(
            track.enabled and (not pending.committed or not self._presented_revision(track, state.run.revision))
            for track in state.reading_tracks
        )
        return drafts + len(state.positions) - len(pending.realizations) + readers

    async def _present_track(
        self, state: Snapshot, provider: Any, reading_id: str, *, reserve_window: bool = True,
    ) -> None:
        track = reading_track_for(state, reading_id)
        if self._presented_revision(track, state.run.revision):
            return
        frame = (state.pending.reading_frames.get(reading_id) if state.pending is not None else None)
        frame = frame or self.reading_frame(state, reading_id)
        if state.run.mode == "reflexive_v2":
            frozen = existing_request(state, "present", reading_scope(reading_id))
            if frozen:
                frame = ReadingFrame.model_validate(frozen.frame)
        try:
            result = await self._format_call(
                state, frame, provider, "present", f"present:{reading_id}", reserve_window=reserve_window,
            )
            self.apply_presentation(state, result.value, reading_id, frame=frame)
            self._accept_request(state, "present", reading_scope(reading_id), result.value)
            if state.pending is not None and state.pending.committed:
                state.pending.presentations[reading_id] = result.value
            self.event(
                state, "presented", reading_id=reading_id, segment_id=track.story[-1].id,
                reinterprets=result.value.reinterprets, presentation=result.value.model_dump(),
                manual=not reserve_window,
                **self._accepted_evidence(state, "present", reading_scope(reading_id)),
                **({"reflection_frame": frame.reflection.model_dump()} if frame.reflection else {}),
            )
        except (KernelError, ProviderError) as error:
            track.last_error = {"code": error.code, "message": error.message, "retryable": error.retryable}
            self.store.save(state)
            raise

    async def present_reading(self, run_id: str, reading_id: str) -> Snapshot:
        # Manual serialization shares the world's lock and budget, but never reruns actors.
        self._stable(run_id)
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            state = self.store.load(run_id)
            track = reading_track_for(state, reading_id)
            if self._presented_revision(track, state.run.revision):
                accepted = next((event for event in reversed(state.events) if event.type == "presented"
                                 and event.data.get("reading_id", "main") == reading_id
                                 and event.revision == state.run.revision), None)
                if state.reflection is not None and accepted and (
                    accepted.data.get("manual") or state.pending is None or not state.pending.committed
                ):
                    self._install_reading_feedback(state, [accepted.data])
                    track.last_error = None
                    self.store.save(state)
                return state
            if not self._world_ready(state):
                raise KernelError("READING_NOT_READY", "请先完成本世界的共同构想和各人物续延，再从当前世界生成阅读。")
            provider = make_provider(state.run.provider, self.settings)
            previous_status, previous_phase = state.run.status, state.run.phase
            state.run.status, state.run.phase = "running", "presenting"
            self.store.save(state)
            try:
                await self._present_track(state, provider, reading_id, reserve_window=False)
                if state.reflection is not None:
                    # The accepted event is the executable checkpoint for manual feedback.
                    accepted = next(event for event in reversed(state.events)
                                    if event.type == "presented" and event.data.get("reading_id") == reading_id)
                    self._install_reading_feedback(state, [accepted.data])
                return state
            except (KernelError, ProviderError):
                self.event(state, "reading_error", reading_id=reading_id, **track.last_error)
                raise
            except asyncio.CancelledError:
                # _call already settles an interrupted request conservatively.
                raise
            except Exception:
                track.last_error = {
                    "code": "RUNTIME_FAILURE", "message": "当前阅读未完成，可从本世界已保存结果重新呈现。",
                    "retryable": True,
                }
                self.event(state, "reading_error", reading_id=reading_id, **track.last_error)
                raise ProviderError("RUNTIME_FAILURE", track.last_error["message"], True) from None
            finally:
                state.run.status = "paused" if run_id in self.pause_requests else previous_status
                state.run.phase = previous_phase
                self.store.save(state)

    def _token_estimate(self, state: Snapshot, frame: Any, projection: dict | None = None) -> int:
        import json

        text = json.dumps(projection, ensure_ascii=False) if projection is not None else frame.model_dump_json()
        estimate = len(text.encode("utf-8")) + 4096 + self.settings.max_output_tokens
        if state.run.provider == "fixture":
            estimate = 0  # Deterministic local execution makes no token-billed call.
        return estimate

    async def _format_call(
        self, state: Snapshot, frame: Any, provider: Any, method: str, job_key: str,
        *, reserve_window: bool = True,
    ) -> ModelResult[Any]:
        scope = reading_scope(job_key.split(":", 1)[1]) if method == "present" else position_scope(frame.position_id)
        existing = existing_request(state, method, scope) if state.run.mode == "reflexive_v2" else None
        if existing is not None and callable(getattr(provider, "with_projection_version", None)):
            # Keep previously frozen valid requests byte-for-byte stable after adapter upgrades.
            provider = provider.with_projection_version(existing.projection_version)
        projector = getattr(provider, "request_projection", None)
        base_projection = projector(frame, method) if callable(projector) else None
        envelope = None
        if state.run.mode == "reflexive_v2":
            if state.experiment and state.experiment.get("execution_signature") != execution_signature(
                state.run.provider, self.settings,
            ):
                raise KernelError("EXPERIMENT_CONFIGURATION_CHANGED", "对照实验的程序或模型配置已改变，请重新建立配对。")
            observe_job(state, frame, provider, method, scope, projection=base_projection)
            already_frozen = existing_request(state, method, scope) is not None
            envelope = freeze_request(state, frame, provider, method, scope, projection=base_projection)
            if not already_frozen:
                self.event(state, "request_frozen", request_id=envelope.id, stage=method,
                           scope_id=scope, frame=frame.model_dump(), frame_hash=envelope.frame_hash)
            else:
                self.store.save(state)
        feedback = None
        for attempt in range(MAX_FORMAT_REPAIRS + 1):
            try:
                evaluate = getattr(provider, method)
                call = (lambda: evaluate(frame)) if feedback is None else (lambda: evaluate(frame, repair=feedback))
                if callable(projector):
                    projection = base_projection if feedback is None else projector(frame, method, repair=feedback)
                else:
                    projection = frame.model_dump() if envelope is not None else None
                return await self._call(
                    state, frame, call, job_key=job_key, envelope=envelope, projection=projection,
                    repair_category=feedback.category if feedback else None,
                )
            except ProviderError as error:
                feedback = error.feedback
                repairable = (
                    error.code == "MODEL_OUTPUT_INVALID" and error.retryable and feedback is not None
                    and (
                        feedback.category in {"JSON_SYNTAX", "JSON_TYPE", "SCHEMA_VALIDATION"}
                        or (method == "propose" and feedback.category == "PROPOSAL_REFERENCE"
                            and bool(feedback.issues) and set(feedback.issues) <= {
                                "TARGET_UNKNOWN", "EXPRESSION_UNAVAILABLE", "INTERFACE_UNKNOWN",
                            })
                        or (method in {"propose", "present"} and state.reflection is not None
                            and feedback.category == "PROPOSAL_INVALID"
                            and feedback.issues == ("REFLECTION_REFERENCE_INVALID",))
                        or (method == "present" and feedback.category == "READING_INVALID"
                            and bool(feedback.issues) and set(feedback.issues) <= {
                                "READING_SCOPE_INVALID", "READING_FOCUS_INVALID", "READING_EXPRESSION_INVALID",
                                "READING_HISTORY_INVALID", "READING_SHARED_REQUIRED",
                            })
                    )
                )
                if not repairable or attempt >= MAX_FORMAT_REPAIRS or state.run.id in self.pause_requests:
                    raise
                # In-flight base calls have already consumed call slots but have not
                # reached their checkpoints. Do not double-count them as future work.
                required = max(1, self._needed_calls(state) - len(self._funded_jobs.get(state.run.id, set())))
                if not reserve_window:
                    required = 1
                usage = state.run.usage
                occupied = usage.input_tokens + usage.output_tokens + usage.unknown_tokens + usage.reserved_tokens
                repair_projection = None
                if envelope is not None and callable(getattr(provider, "request_projection", None)):
                    repair_projection = provider.request_projection(frame, method, repair=feedback)
                if ((state.run.limits.max_calls is not None
                     and usage.calls + required > state.run.limits.max_calls)
                        or (state.run.limits.max_tokens is not None
                            and occupied + self._token_estimate(state, frame, repair_projection)
                            > state.run.limits.max_tokens)):
                    raise
                self.event(
                    state, "format_retry", method=method, job=job_key, attempt=attempt + 1,
                    category=feedback.category, issues=list(feedback.issues),
                )
        raise AssertionError("Bounded format repair exhausted without an outcome")

    async def _call(
        self, state: Snapshot, frame: Any, call: Callable[[], Awaitable[ModelResult[T]]],
        *, job_key: str | None = None,
        envelope: RequestEnvelope | None = None, projection: dict | None = None,
        repair_category: str | None = None,
    ) -> ModelResult[T]:
        usage, limits = state.run.usage, state.run.limits
        estimate = self._token_estimate(state, frame, projection)
        used = usage.input_tokens + usage.output_tokens + usage.unknown_tokens + usage.reserved_tokens
        if ((limits.max_calls is not None and usage.calls >= limits.max_calls)
                or (limits.max_tokens is not None and used + estimate > limits.max_tokens)):
            raise KernelError("BUDGET_EXHAUSTED", "预算不足以安全发起下一次调用；已完成的阶段已保存。")
        usage.calls += 1
        usage.reserved_tokens += estimate
        attempt = None
        if envelope is not None:
            attempt = RequestAttempt(
                number=len(envelope.attempts) + 1, request_hash=fingerprint(projection),
                projection=projection, repair_category=repair_category,
                request_chars=request_chars(projection),
            )
            envelope.attempts.append(attempt)
        self.store.save(state)
        funded = self._funded_jobs.setdefault(state.run.id, set())
        if job_key is not None:
            funded.add(job_key)
        try:
            result = await call()
        except BaseException as error:
            usage.reserved_tokens -= estimate
            # A rejected output can still carry trustworthy provider billing counts.
            # Only transport/envelope failures with unknown usage retain the reservation.
            known_usage = error.usage if isinstance(error, ProviderError) else None
            if known_usage is None:
                usage.unknown_tokens += estimate
            else:
                usage.input_tokens += known_usage[0]
                usage.output_tokens += known_usage[1]
            usage.failed_calls += 1
            if attempt is not None:
                attempt.status = "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
                attempt.error_code = error.code if isinstance(error, ProviderError) else "REQUEST_INTERRUPTED"
                if known_usage is not None:
                    attempt.input_tokens, attempt.output_tokens = known_usage
            self.store.save(state)
            raise
        finally:
            if job_key is not None:
                funded.discard(job_key)
        usage.reserved_tokens -= estimate
        usage.input_tokens += result.input_tokens
        usage.output_tokens += result.output_tokens
        if attempt is not None:
            attempt.status = "received"
            attempt.input_tokens, attempt.output_tokens = result.input_tokens, result.output_tokens
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
                if (state.run.limits.max_windows is not None
                        and state.run.usage.windows >= state.run.limits.max_windows):
                    raise KernelError("BUDGET_EXHAUSTED", "本实验的局部窗口额度已用完，可提高窗口预算后继续。")
                if (state.run.limits.max_calls is not None
                        and state.run.usage.calls + self._needed_calls(state) > state.run.limits.max_calls):
                    raise KernelError("BUDGET_EXHAUSTED", "剩余调用额度不足以完成一个窗口；没有发起新调用。")
                if state.pending is None:
                    state.pending = Pending(base_revision=state.run.revision, epoch_id=uuid4().hex,
                                            policy_revision=state.policy_revision)
                    state.run.phase = "drafting"
                    self.event(state, "window_started", base_revision=state.run.revision)
                pending = state.pending
                if not pending.epoch_id:
                    pending.epoch_id = uuid4().hex
                    pending.policy_revision = state.policy_revision
                if state.run.mode == "reflexive_v2" and (
                    state.schedule is None or state.schedule.epoch_id != pending.epoch_id
                ):
                    begin_plan(state)
                if not pending.committed:
                    missing = {p.id: frame_for(state, p.id) for p in state.positions
                               if p.id not in pending.proposal_frames}
                    if missing:
                        pending.proposal_frames.update(missing)
                        self.event(state, "proposal_inputs_frozen", frames={
                            key: value.model_dump() for key, value in missing.items()
                        }, reconstructed=[key for key in missing if key in pending.proposals])
                semaphore = asyncio.Semaphore(state.run.limits.max_parallel or max(
                    1, len(state.positions) + sum(track.enabled for track in state.reading_tracks),
                ))

                async def draft(position_id: str) -> None:
                    if position_id in pending.proposals:
                        return
                    async with semaphore:
                        frame = pending.proposal_frames[position_id]
                        result = await self._format_call(state, frame, provider, "propose", f"propose:{position_id}")
                        if result.value.position_id != position_id:
                            raise KernelError("MODEL_OUTPUT_INVALID", "模型返回了不匹配的生成位置。")
                        validate_proposal(state, result.value, frame=frame)
                        pending.proposals[position_id] = result.value
                        self._accept_request(state, "propose", position_scope(position_id), result.value)
                        self.event(
                            state,
                            "proposal_ready",
                            position_id=position_id,
                            based_on_revision=result.value.based_on_revision,
                            **({"proposal": result.value.model_dump(),
                                **self._accepted_evidence(state, "propose", position_scope(position_id))}
                               if state.run.mode == "reflexive_v2" else {}),
                        )

                if not pending.committed:
                    results = await asyncio.gather(
                        *(draft(p.id) for p in state.positions), return_exceptions=True
                    )
                    failure = next((r for r in results if isinstance(r, BaseException)), None)
                    if failure:
                        raise failure
                    try:
                        state = install(state, list(pending.proposals.values()), pending.base_revision,
                                        proposal_frames=pending.proposal_frames)
                    except KernelError:
                        # Invalid collective drafts are not cached forever across retry.
                        state.pending = Pending(base_revision=state.run.revision, epoch_id=uuid4().hex,
                                                policy_revision=state.policy_revision,
                                                deferred_releases=pending.deferred_releases)
                        self.store.save(state)
                        raise
                    pending = state.pending
                    assert pending is not None
                    pending.committed = True
                    if state.reflection is not None:
                        pending.realization_frames = {p.id: frame_for(state, p.id) for p in state.positions}
                    state.run.phase = "realizing"
                    self.event(
                        state,
                        "batch_installed",
                        changes=state.audit["condition_changes"],
                        joint_targets=state.audit["joint_targets"],
                        base_revision=pending.base_revision,
                        proposals=[p.model_dump() for p in pending.proposals.values()],
                        proposal_frames={key: value.model_dump() for key, value in pending.proposal_frames.items()},
                        **({"realization_frames": {key: value.model_dump()
                                                  for key, value in pending.realization_frames.items()}}
                           if state.reflection else {}),
                    )
                if run_id in self.pause_requests:
                    state.run.status = "paused"
                    self.event(state, "paused", boundary="after_install")
                    return state

                async def realize(position_id: str) -> None:
                    if position_id in pending.realizations:
                        return
                    async with semaphore:
                        frame = pending.realization_frames.get(position_id) or frame_for(state, position_id)
                        result = await self._format_call(state, frame, provider, "realize", f"realize:{position_id}")
                        if not result.value.strip():
                            raise KernelError("MODEL_OUTPUT_INVALID", "恢复生成没有返回可读内容。")
                        pending.realizations[position_id] = result.value
                        self._accept_request(state, "realize", position_scope(position_id), result.value)
                        continuation = next(c for c in state.continuations if c.position_id == position_id)
                        continuation.surface = result.value
                        continuation.status = "ready"
                        continuation.resume_count += 1
                        choice = capture_choice(state, position_scope(position_id), "realize", frame.reflection)
                        self.event(
                            state,
                            "continuation_resumed",
                            position_id=position_id,
                            frame_revision=frame.revision,
                            condition_fingerprint=frame.condition_fingerprint,
                            surface=result.value,
                            **self._accepted_evidence(state, "realize", position_scope(position_id)),
                            **({"choice": choice.model_dump()} if choice else {}),
                        )

                results = await asyncio.gather(
                    *(realize(p.id) for p in state.positions), return_exceptions=True
                )
                failure = next((r for r in results if isinstance(r, BaseException)), None)
                if failure:
                    raise failure
                state.run.phase = "presenting"
                if state.reflection is not None:
                    fresh_frames = {
                        track.id: self.reading_frame(state, track.id)
                        for track in state.reading_tracks if track.enabled and track.id not in pending.reading_frames
                    }
                    if fresh_frames:
                        pending.reading_frames.update(fresh_frames)
                        self.event(state, "reading_inputs_frozen", frames={
                            key: value.model_dump() for key, value in fresh_frames.items()
                        })
                self.store.save(state)
                if run_id in self.pause_requests:
                    state.run.status = "paused"
                    self.event(state, "paused", boundary="before_presentation")
                    return state
                async def present(reading_id: str) -> None:
                    async with semaphore:
                        await self._present_track(state, provider, reading_id)

                results = await asyncio.gather(
                    *(present(track.id) for track in state.reading_tracks if track.enabled),
                    return_exceptions=True,
                )
                failure = next((result for result in results if isinstance(result, BaseException)), None)
                if failure:
                    raise failure
                if state.reflection is not None:
                    self._install_reading_feedback(state, [
                        {"reading_id": reading_id, "presentation": presentation.model_dump(),
                         "reflection_frame": pending.reading_frames[reading_id].reflection.model_dump()}
                        for reading_id, presentation in pending.presentations.items()
                        if pending.reading_frames.get(reading_id) is not None
                    ])
                for release in pending.deferred_releases:
                    if state.reflection and release["method_id"] in state.reflection.adoptions.get(release["scope_id"], []):
                        state.reflection.adoptions[release["scope_id"]].remove(release["method_id"])
                        state.policy_revision += 1
                        self.event(state, "method_released", **release)
                state.pending = None
                state.run.usage.windows += 1
                state.run.phase = "idle"
                state.run.status = (
                    "paused" if run_id in self.pause_requests else "running" if automatic else "idle"
                )
                self.event(
                    state,
                    "window_completed",
                    reading_ids=[track.id for track in state.reading_tracks if track.enabled],
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

    def reading_frame(self, state: Snapshot, reading_id: str = "main") -> ReadingFrame:
        return reading_frame_for(state, reading_id)

    @staticmethod
    def _accept_request(state: Snapshot, stage: str, scope: str, value: Any) -> None:
        request = existing_request(state, stage, scope) if state.run.mode == "reflexive_v2" else None
        if request is not None:
            request.accepted_result_hash = fingerprint(value.model_dump() if hasattr(value, "model_dump") else value)

    @staticmethod
    def _accepted_evidence(state: Snapshot, stage: str, scope: str) -> dict:
        request = existing_request(state, stage, scope) if state.run.mode == "reflexive_v2" else None
        return {"request_id": request.id, "result_hash": request.accepted_result_hash} if request else {}

    def apply_presentation(
        self, state: Snapshot, presentation: Presentation, reading_id: str = "main",
        *, frame: ReadingFrame | None = None,
    ) -> None:
        from .reflection import validate_reflection

        frame = frame or self.reading_frame(state, reading_id)
        validate_reflection(presentation.reflection, frame.reflection)
        if state.reflection is not None:
            install_feedback(state.model_copy(deep=True), [
                (reading_scope(reading_id), presentation.reflection, frame.reflection),
            ])
        install_presentation(state, presentation, reading_id)
        capture_choice(state, reading_scope(reading_id), "present", frame.reflection)

    def _install_reading_feedback(self, state: Snapshot, entries: list[dict]) -> None:
        keys = ("reading_id", "presentation", "reflection_frame")
        entries = [{key: entry[key] for key in keys} for entry in entries]
        installed = [entry for event in state.events if event.type == "reflection_feedback"
                     for entry in event.data["entries"]]
        entries = [entry for entry in entries if entry not in installed]
        if not entries:
            return
        proposals = [
            (reading_scope(entry["reading_id"]), Presentation.model_validate(entry["presentation"]).reflection,
             ReflectionFrame.model_validate(entry["reflection_frame"]))
            for entry in entries
        ]
        install_feedback(state, proposals)
        self.event(state, "reflection_feedback", entries=sorted(entries, key=lambda item: item["reading_id"]))

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
