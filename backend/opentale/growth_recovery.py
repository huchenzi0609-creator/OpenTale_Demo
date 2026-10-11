"""Zero-provider recovery of historical review-only technical pauses.

An accepted draft remains a private proposal.  Failed review work becomes an
explicit debt; neither a review nor a world event is fabricated to clear the
pause.  Original requests, attempts and resource accounting remain untouched.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from .growth_capabilities import CapabilityError, resolve_handle
from .growth_store import GrowthStore
from .growth_types import GraphState, ObjectRef, SourceSpan


def _identifier(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def _put(store: GrowthStore, run_id: str, kind: str, value: dict, commit: int) -> dict:
    value = dict(value)
    value.pop("version", None)
    return store.put(run_id, kind, value, commit_seq=commit)


def _request(store: GrowthStore, state: GraphState, scope: str, sequence: int, stage: str) -> dict | None:
    return store.get(state.run_id, "request", f"request-{_identifier(scope, sequence, stage)}", scope_id=scope)


def _exact(store: GrowthStore, state: GraphState, scope: str, raw: dict) -> dict | None:
    try:
        ref = ObjectRef.model_validate(raw)
    except (TypeError, ValueError):
        return None
    return store.get(state.run_id, ref.kind, ref.object_id, version=ref.version, scope_id=scope,
                     at_commit=state.graph_commit if ref.kind in {"node", "link"} else None)


def _method_ref(row: dict) -> dict:
    return {"kind": "method", "object_id": row.get("id", ""), "version": row.get("version", "")}


def _targets(store: GrowthStore, state: GraphState, scope: str, request: dict) -> list[dict] | None:
    targets = request.get("frame", {}).get("review_targets")
    if not isinstance(targets, list) or not targets or any(not _exact(store, state, scope, ref) for ref in targets):
        return None
    return targets


def _safe_draft(store: GrowthStore, state: GraphState, scope: str, sequence: int,
                review: dict, targets: list[dict]) -> tuple[dict, dict, dict] | None:
    draft = _request(store, state, scope, sequence, "draft")
    if (not draft or draft.get("stage") != "draft" or draft.get("status") != "accepted"
            or not isinstance(draft.get("result"), str)):
        return None
    if not draft["result"].strip():
        return None
    trial_targets = [target for target in targets if target["kind"] == "trial"]
    if len(trial_targets) != 1:
        return None
    trial = _exact(store, state, scope, trial_targets[0])
    candidate_ref = trial.get("result_ref", {})
    latest_trial = store.get(state.run_id, "trial", trial["id"], scope_id=scope)
    if trial.get("adopted") or (latest_trial or {}).get("adopted") or candidate_ref.get("kind") != "candidate":
        return None
    candidate = _exact(store, state, scope, candidate_ref)
    method_ref = _method_ref(review.get("frame", {}).get("method", {}))
    if (not candidate or candidate.get("request_id") != draft["id"] or not candidate.get("text_ref")
            or candidate.get("method_ref") != method_ref or trial.get("method_ref") != method_ref
            or _method_ref(draft.get("frame", {}).get("method", {})) != method_ref):
        return None
    try:
        if store.read_text(candidate["text_ref"]) != draft["result"]:
            return None
        # Exact body/range checks prevent an accepted but mismatched imported
        # request from blessing a different private proposal.
        for raw_span in candidate.get("source_spans", []):
            span = SourceSpan.model_validate(raw_span)
            source = _exact(store, state, scope, {"kind": "node", "object_id": span.node_id,
                                                "version": span.node_version})
            if not source or source.get("text_ref") != span.text_ref.model_dump():
                return None
            store.read_text(span.text_ref, span.start, span.end)
        for supplied in draft.get("frame", {}).get("sources", []):
            source = _exact(store, state, scope, {"kind": "node", "object_id": supplied["id"],
                                                "version": supplied["version"]})
            if not source:
                return None
            start, end = supplied.get("start", 0), supplied.get("end", len(supplied["text"]))
            if store.read_text(source["text_ref"], start, end) != supplied["text"]:
                return None
    except (ValueError, KeyError, TypeError):
        return None
    return draft, trial, candidate


def _reflect_matches(store: GrowthStore, state: GraphState, scope: str, decision: dict,
                     review: dict, targets: list[dict]) -> bool:
    args, frame = decision["result"].get("args", {}), decision.get("frame", {})
    try:
        method = resolve_handle(frame, args["method_id"], "method", args.get("method_version"))
        expected_method = method.as_dict()
        expected = []
        for key in args.get("about_ids", []):
            value = resolve_handle(frame, key).as_dict()
            if value not in expected:
                expected.append(value)
        for raw in args.get("about_refs", []):
            parsed = ObjectRef.model_validate(raw)
            resolve_handle(frame, parsed.object_id, parsed.kind, parsed.version)
            if parsed.model_dump() not in expected:
                expected.append(parsed.model_dump())
        expected = expected or [expected_method]
        return (expected == targets and expected_method == _method_ref(review["frame"].get("method", {}))
                and _exact(store, state, scope, expected_method) is not None)
    except (CapabilityError, KeyError, ValueError, TypeError):
        return False


def recover_review_pauses(store: GrowthStore, state: GraphState) -> dict:
    """Migrate verified historical ``TECHNICAL_PROSE`` review pauses once.

    Only an error at the next uninstalled accepted decide sequence is eligible.
    Frozen review methods/targets remain exact.  The advanced sequence receipt
    says deferred and ``work_completed: false``; the original review can later
    be attempted through an explicit debt.  No provider is constructed or used.
    """
    recovered, skipped = [], []
    with store.transaction():
        current = store.load_head(state.run_id)
        if current is None:
            return {"recovered": [], "skipped": [], "recovered_count": 0}
        for window_id, actor in current.windows.items():
            scope = f"window:{window_id}"
            if actor.status != "error":
                continue
            session = store.get(current.run_id, "session", f"session-{_identifier(scope)}", scope_id=scope)
            if not session or session.get("status") != "error":
                continue
            error = session.get("error", {}) or {}
            if not isinstance(error, dict) or type(session.get("work_seq", 0)) is not int:
                skipped.append({"scope_id": scope, "reason": "invalid_session"})
                continue
            sequence = session.get("work_seq", 0) + 1
            review = _request(store, current, scope, sequence, "review")
            decision = _request(store, current, scope, sequence, "decide")
            feedback = (review or {}).get("validation_feedback") or {}
            issues = feedback.get("issues", []) if isinstance(feedback, dict) else []
            session_feedback = error.get("validation_feedback") or {}
            session_issues = session_feedback.get("issues", []) if isinstance(session_feedback, dict) else []
            if (not review or review.get("stage") != "review" or review.get("status") != "failed"
                    or "TECHNICAL_PROSE" not in issues or "TECHNICAL_PROSE" not in session_issues
                    or not decision or decision.get("stage") != "decide" or decision.get("status") != "accepted"
                    or not isinstance(decision.get("result"), dict)):
                skipped.append({"scope_id": scope, "reason": "not_verified_review_pause"})
                continue
            if not isinstance(session.get("review_debt", []), list):
                skipped.append({"scope_id": scope, "reason": "invalid_review_debt"})
                continue
            action = decision["result"].get("action")
            targets = _targets(store, current, scope, review)
            method_ref = _method_ref(review.get("frame", {}).get("method", {}))
            if not targets or not _exact(store, current, scope, method_ref):
                skipped.append({"scope_id": scope, "reason": "review_material_unavailable"})
                continue
            draft_state = None
            if action == "try_write":
                draft_state = _safe_draft(store, current, scope, sequence, review, targets)
                eligible = draft_state is not None
            elif action == "reflect":
                eligible = _reflect_matches(store, current, scope, decision, review, targets)
            else:
                eligible = False
            if not eligible:
                skipped.append({"scope_id": scope, "reason": "saved_work_mismatch"})
                continue
            debt_id = "review-debt-" + _identifier(scope, sequence, review["id"])
            safe_error = {key: deepcopy(error[key]) for key in ("code", "message", "validation_feedback") if key in error}
            debt = {"id": debt_id, "status": "pending", "stage": "review", "request_id": review["id"],
                    "targets": deepcopy(targets), "error": safe_error, "method_ref": method_ref,
                    "automatic": action == "try_write", "purpose": review["frame"].get("purpose", ""),
                    "sequence": sequence, "phase": "review_pending",
                    "recovery": "historical_technical_review_pause"}
            debts = session.setdefault("review_debt", [])
            if not any(row.get("id") == debt_id for row in debts):
                debts.append(debt)
            details = {"status": "deferred", "work_completed": False, "phase": "review_pending",
                       "review_debt_id": debt_id, "changed": False}
            if draft_state:
                draft, trial, candidate = draft_state
                saved_draft_ref = {"kind": "candidate", "object_id": candidate["id"], "version": candidate["version"]}
                debt.update(saved_draft_ref=saved_draft_ref, trial_id=trial["id"], draft_request_id=draft["id"],
                            phase="review_pending")
                session.update(pending_trial_id=trial["id"], saved_draft_ref=saved_draft_ref,
                               phase="review_pending")
                details.update(trial_id=trial["id"], saved_draft_ref=saved_draft_ref, draft_request=draft["id"])
            session.update(work_seq=sequence, last_action=action, status="ready", error=None, phase="review_pending")
            actor.status = "ready"
            _put(store, current.run_id, "receipt", {"id": f"receipt-{_identifier(scope, sequence)}",
                "session_id": session["id"], "scope_id": scope, "private_scope": scope,
                "action": action, "args": decision["result"].get("args", {}), "details": details,
                "graph_commit": current.graph_commit}, current.graph_commit)
            _put(store, current.run_id, "session", session, current.graph_commit)
            _put(store, current.run_id, "event", {"id": "finished-work-" + _identifier(scope, sequence),
                "action": "finished_work", "graph_commit": current.graph_commit,
                "details": {"scope_id": scope, "work_seq": sequence, "decision_action": action, **details}},
                current.graph_commit)
            store.record_event(current.run_id, "review_pause_recovered", {
                "scope_id": scope, "work_seq": sequence, "review_request_id": review["id"],
                "review_debt_id": debt_id, "work_completed": False})
            recovered.append({"scope_id": scope, "sequence": sequence, "action": action,
                              "review_debt_id": debt_id, "saved_draft": draft_state is not None})
        if recovered:
            actors = [*current.windows.values(), *current.reading_tracks.values()]
            if all(actor.status not in {"error", "calling", "running"} for actor in actors):
                current.status = "idle"
            store.save_head(current)
    return {"recovered": recovered, "skipped": skipped, "recovered_count": len(recovered)}
