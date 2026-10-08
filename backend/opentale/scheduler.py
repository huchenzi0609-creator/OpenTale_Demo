"""Dependency observations alongside explicit narrative advancement (shadow rollout)."""

from .domain import Snapshot
from .kernel import fingerprint
from .request_types import ScheduleJob, SchedulePlan


def begin_plan(state: Snapshot) -> None:
    if state.run.mode == "reflexive_v2" and state.pending:
        state.schedule = SchedulePlan(epoch_id=state.pending.epoch_id)


def observe_job(state: Snapshot, frame, provider, stage: str, scope_id: str, *, projection: dict | None = None) -> None:
    if state.schedule is None:
        return
    key = f"{stage}:{scope_id}"
    if any(job.key == key for job in state.schedule.jobs):
        return
    project = getattr(provider, "request_projection", None)
    body = projection if projection is not None else project(frame, stage) if callable(project) else frame.model_dump()
    # Operational revision numbers do not establish a change in narrative conditions.
    if isinstance(body, dict) and "fixture_version" in body:
        body = {key: value for key, value in body.items() if key != "revision"}
    previous = next((request for request in reversed(state.requests)
                     if request.stage == stage and request.scope_id == scope_id
                     and request.epoch_id != state.schedule.epoch_id), None)
    old_body = previous.attempts[0].projection if previous and previous.attempts else None
    if isinstance(old_body, dict) and "fixture_version" in old_body:
        old_body = {key: value for key, value in old_body.items() if key != "revision"}
    changed = old_body is None or fingerprint(body) != fingerprint(old_body)
    dependencies = list(getattr(frame.reflection, "live_dependencies", [])) if frame.reflection else []
    state.schedule.jobs.append(ScheduleJob(
        key=key, stage=stage, scope_id=scope_id, changed=changed,
        reason="首次生成" if previous is None else "输入变化与主动推进" if changed else "主动继续叙述",
        dependencies=sorted(set(dependencies)),
    ))
