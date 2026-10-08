"""Request identity is frozen before transport; repairs remain separately inspectable attempts."""

import hashlib
import json
from pathlib import Path
from typing import Any

from .domain import Snapshot
from .kernel import KernelError, fingerprint
from .request_types import ContextCompaction, RequestEnvelope


def context_compaction_for(projection: dict) -> ContextCompaction | None:
    for message in projection.get("messages", []):
        if message.get("role") != "user":
            continue
        data = json.loads(message["content"])
        if isinstance(data, dict) and data.get("context_compaction") is not None:
            return ContextCompaction.model_validate(data["context_compaction"])
    return None


def request_chars(projection: dict | None) -> int | None:
    if projection is None or "messages" not in projection:
        return None
    return sum(len(message.get("content", "")) for message in projection["messages"])


def execution_signature(provider: str, settings: Any) -> str:
    """Hash code and non-secret generation settings without publishing endpoints or secrets."""
    directory = Path(__file__).parent
    sources = {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(directory.rglob("*.py"))}
    configuration = ({"model": settings.model, "max_output_tokens": settings.max_output_tokens,
                      "context_target_chars": getattr(settings, "context_target_chars", 96000),
                      "endpoint": settings.base_url} if provider == "deepseek" else {"provider": "fixture"})
    return fingerprint({"sources": sources, "configuration": configuration})


def request_identity(state: Snapshot, stage: str, scope_id: str) -> tuple[str, str]:
    pending = state.pending
    epoch = pending.epoch_id if pending and (stage != "present" or pending.committed) else (
        f"manual:{state.run.revision}"
    )
    return f"request-{fingerprint([epoch, stage, scope_id])[:24]}", epoch


def existing_request(state: Snapshot, stage: str, scope_id: str) -> RequestEnvelope | None:
    identifier, _ = request_identity(state, stage, scope_id)
    return next((request for request in state.requests if request.id == identifier), None)


def freeze_request(
    state: Snapshot, frame: Any, provider: Any, stage: str, scope_id: str,
    *, projection: dict | None = None,
) -> RequestEnvelope:
    """Caller persists before any attempt. An edited prompt/config cannot relabel old evidence."""
    identifier, epoch = request_identity(state, stage, scope_id)
    projector = getattr(provider, "request_projection", None)
    if projection is None:
        projection = projector(frame, stage) if callable(projector) else frame.model_dump()
    frame_hash, request_hash = fingerprint(frame.model_dump()), fingerprint(projection)
    existing = existing_request(state, stage, scope_id)
    if existing:
        if existing.frame_hash != frame_hash:
            raise KernelError("REQUEST_INPUT_CHANGED", "已冻结请求的输入发生变化，请从原恢复点继续。")
        if existing.request_hash != request_hash:
            raise KernelError("REQUEST_CONFIGURATION_CHANGED", "恢复请求的模型或提示配置已改变，请恢复原配置再继续。")
        return existing
    envelope = RequestEnvelope(
        id=identifier, epoch_id=epoch, stage=stage, scope_id=scope_id,
        input_world_revision=frame.revision,
        policy_revision=state.policy_revision,
        frame=frame.model_dump(), frame_hash=frame_hash, request_hash=request_hash,
        projection_version=getattr(provider, "projection_version", "provider-json-v1")
        if callable(projector) else "frame-v1",
        evidence_level="exact_projection" if callable(projector) else "frame_only",
        context_compaction=context_compaction_for(projection),
    )
    state.requests.append(envelope)
    return envelope


def verify_request_evidence(state: Snapshot) -> bool:
    accepted = {}
    for event in state.events:
        key = {"proposal_ready": "proposal", "continuation_resumed": "surface", "presented": "presentation"}.get(
            event.type,
        )
        if key and event.data.get("request_id"):
            if key not in event.data or fingerprint(event.data[key]) != event.data.get("result_hash"):
                return False
            identifier = event.data["request_id"]
            if identifier in accepted:
                return False
            accepted[identifier] = event.data["result_hash"]
    ids = set()
    for request in state.requests:
        if request.id in ids or fingerprint(request.frame) != request.frame_hash:
            return False
        ids.add(request.id)
        if request.frame.get("revision") != request.input_world_revision:
            return False
        if request.accepted_result_hash is not None and (
            not request.attempts or request.attempts[-1].status != "received"
            or accepted.get(request.id) != request.accepted_result_hash
        ):
            return False
        for number, attempt in enumerate(request.attempts, start=1):
            if attempt.number != number or attempt.projection is None:
                return False
            if fingerprint(attempt.projection) != attempt.request_hash:
                return False
            if attempt.request_chars is not None and request_chars(attempt.projection) != attempt.request_chars:
                return False
            if context_compaction_for(attempt.projection) != request.context_compaction:
                return False
            if request.context_compaction is not None:
                if request.context_compaction.algorithm == "source-pages-v1":
                    from .providers.context_bounded import verify_bounded_sources

                    sources_valid = verify_bounded_sources(request.frame, attempt.projection)
                else:
                    from .providers.context import verify_compaction_sources

                    sources_valid = verify_compaction_sources(request.frame, attempt.projection)
                if not sources_valid:
                    return False
            if attempt.repair_category is None and attempt.request_hash != request.request_hash:
                return False
            if attempt.status == "received" and (attempt.input_tokens is None or attempt.output_tokens is None):
                return False
    if not set(accepted) <= ids:
        return False
    return True
