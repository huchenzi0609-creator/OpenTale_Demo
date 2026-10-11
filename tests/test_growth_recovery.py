"""Historical review failures release safe drafts without invented reviews or calls."""

from __future__ import annotations

from copy import deepcopy

import pytest
from opentale.growth_kernel import add_node
from opentale.growth_recovery import _identifier, recover_review_pauses
from opentale.growth_store import GrowthStore
from opentale.growth_types import GraphState, MethodVersion, SourceSpan, WindowState

from scripts.recover_growth_reviews import RecoveryFailure, recover_database


@pytest.fixture
def paused(tmp_path):
    store = GrowthStore(tmp_path)
    state = GraphState(run_id="review-recovery", status="error", windows={
        key: WindowState(id=key) for key in ("a", "b", "c", "d", "e")},
        config={"usage": {"calls": 37, "failed_calls": 15, "input_tokens": 1200,
                          "output_tokens": 800, "unknown_tokens": 0, "reserved_tokens": 0}})
    store.create(state)
    for key, actor in state.windows.items():
        scope = f"window:{key}"
        state = add_node(store, state, key, "她将钥匙交给邻居。", request_id=f"seed:{key}")
        actor = state.windows[key]
        actor.status = "error"
        method = store.put(state.run_id, "method", MethodVersion(id=f"method-{key}", scope_id=scope,
            private_scope=scope, instruction="从钥匙交付以后发生的新生活继续。"))
        method_ref = {"kind": "method", "object_id": method["id"], "version": method["version"]}
        action = "reflect" if key in {"d", "e"} else "try_write"
        sequence = 3
        error = {"code": "MODEL_OUTPUT_INVALID", "message": "正文包含不允许的技术引用。",
                 "validation_feedback": {"category": "PROSE_INVALID", "issues": ["TECHNICAL_PROSE"]}}
        session = {"id": f"session-{_identifier(scope)}", "scope_id": scope, "private_scope": scope,
            "work_seq": sequence - 1, "status": "error", "error": error}
        decide_id = f"request-{_identifier(scope, sequence, 'decide')}"
        args = {"method_id": method["id"]} if action == "reflect" else {"parent_ids": [actor.working_node_id]}
        store.put(state.run_id, "request", {"id": decide_id, "scope_id": scope, "private_scope": scope,
            "stage": "decide", "status": "accepted", "result": {"action": action, "args": args},
            "frame": {"method": method, "work_protocol": "growth-work-v2"}})
        targets = [method_ref]
        if action == "try_write":
            source = store.get(state.run_id, "node", actor.working_node_id)
            accepted = "邻居在午后出门，她为他留下了门。"
            draft_id = f"request-{_identifier(scope, sequence, 'draft')}"
            store.put(state.run_id, "request", {"id": draft_id, "scope_id": scope, "private_scope": scope,
                "stage": "draft", "status": "accepted", "result": accepted,
                "frame": {"method": method, "sources": [{"id": source["id"], "version": source["version"],
                    "text": store.read_text(source["text_ref"]), "start": 0, "end": source["text_ref"]["length"]}]}})
            span = SourceSpan(node_id=source["id"], node_version=source["version"],
                text_ref=source["text_ref"], start=0, end=source["text_ref"]["length"]).model_dump()
            candidate = store.put(state.run_id, "candidate", {"id": f"candidate-{key}", "scope_id": scope,
                "private_scope": scope, "request_id": draft_id, "method_ref": method_ref,
                "text_ref": store.put_text(accepted).model_dump(), "source_spans": [span]})
            trial = store.put(state.run_id, "trial", {"id": f"trial-{key}", "scope_id": scope,
                "private_scope": scope, "method_ref": method_ref, "adopted": False, "feedback_refs": [],
                "result_ref": {"kind": "candidate", "object_id": candidate["id"], "version": candidate["version"]}})
            targets.insert(0, {"kind": "trial", "object_id": trial["id"], "version": trial["version"]})
            session["pending_trial_id"] = trial["id"]
        review_id = f"request-{_identifier(scope, sequence, 'review')}"
        store.put(state.run_id, "request", {"id": review_id, "scope_id": scope, "private_scope": scope,
            "stage": "review", "status": "failed", "error_code": "MODEL_OUTPUT_INVALID",
            "validation_feedback": error["validation_feedback"], "attempts": [{"status": "failed",
                "error_code": "MODEL_OUTPUT_INVALID"}],
            "frame": {"method": method, "review_targets": targets, "purpose": "打开新的行动可能性"}})
        store.put(state.run_id, "session", session)
        store.save_head(state)
    store.save_head(state)
    yield store, state
    store.close()


def objects(store, state, kind):
    return store.list(state.run_id, kind, include_private=True, limit=100)


def test_five_paused_windows_recover_with_exact_safe_outputs_and_explicit_pending_debts(paused):
    store, state = paused
    originals = {kind: deepcopy(objects(store, state, kind)) for kind in ("request", "trial", "candidate", "node")}
    usage = deepcopy(state.config["usage"])
    report = recover_review_pauses(store, state)
    assert report["recovered_count"] == 5
    state = store.load_head(state.run_id)
    assert state.status == "idle" and all(actor.status == "ready" for actor in state.windows.values())
    assert state.config["usage"] == usage
    assert store.count_objects(state.run_id, "response") == 0
    for kind, rows in originals.items():
        assert objects(store, state, kind) == rows
    for session in objects(store, state, "session"):
        assert session["work_seq"] == 3 and session["status"] == "ready" and session["error"] is None
        assert session["phase"] == "review_pending"
        debt = session["review_debt"][0]
        assert debt["status"] == "pending" and debt["stage"] == "review"
        assert debt["error"]["validation_feedback"]["issues"] == ["TECHNICAL_PROSE"]
        assert debt["sequence"] == 3
        if debt["automatic"]:
            assert session["phase"] == "review_pending"
            candidate = store.get(state.run_id, "candidate", session["saved_draft_ref"]["object_id"],
                                  version=session["saved_draft_ref"]["version"], scope_id=session["scope_id"])
            assert store.read_text(candidate["text_ref"]) == "邻居在午后出门，她为他留下了门。"
        else:
            assert "saved_draft_ref" not in session
    assert all(receipt["details"]["status"] == "deferred" and not receipt["details"]["work_completed"]
               for receipt in objects(store, state, "receipt"))
    assert all(event["action"] == "finished_work" and not event["details"]["work_completed"]
               for event in objects(store, state, "event"))


def test_recovery_is_idempotent_without_rewriting_requests_or_rebilling(paused):
    store, state = paused
    recover_review_pauses(store, state)
    before = {kind: deepcopy(objects(store, state, kind)) for kind in ("request", "session", "receipt", "event")}
    before_head = store.load_head(state.run_id).model_dump()
    assert recover_review_pauses(store, state)["recovered_count"] == 0
    assert store.load_head(state.run_id).model_dump() == before_head
    for kind, rows in before.items():
        assert objects(store, state, kind) == rows


@pytest.mark.parametrize("corruption", ["mismatched_draft", "adopted_trial", "different_review", "failed_draft"])
def test_safe_draft_recovery_rejects_unverified_or_changed_saved_material(paused, corruption):
    store, state = paused
    if corruption == "adopted_trial":
        row = store.get(state.run_id, "trial", "trial-a", scope_id="window:a")
        row.pop("version")
        row["adopted"] = True
        store.put(state.run_id, "trial", row)
        # The failed request names the original exact trial. An adopted current
        # version also makes publication visible in current work and must block
        # an older proposal's resurrection.
        review_id = f"request-{_identifier('window:a', 3, 'review')}"
        request = store.get(state.run_id, "request", review_id, scope_id="window:a")
        latest = store.get(state.run_id, "trial", "trial-a", scope_id="window:a")
        request.pop("version")
        request["frame"]["review_targets"][0]["version"] = latest["version"]
        store.put(state.run_id, "request", request)
    else:
        stage = "review" if corruption == "different_review" else "draft"
        request_id = f"request-{_identifier('window:a', 3, stage)}"
        request = store.get(state.run_id, "request", request_id, scope_id="window:a")
        request.pop("version")
        if corruption == "different_review":
            request["validation_feedback"] = {"category": "PROSE_INVALID", "issues": ["EMPTY"]}
        elif corruption == "failed_draft":
            request["status"] = "failed"
        else:
            request["result"] = "另一份不同的草稿。"
        store.put(state.run_id, "request", request)
    report = recover_review_pauses(store, state)
    assert report["recovered_count"] == 4
    a = store.get(state.run_id, "session", f"session-{_identifier('window:a')}", scope_id="window:a")
    assert a["status"] == "error" and a["work_seq"] == 2
    assert "review_debt" not in a


def test_recovery_preserves_concurrently_updated_resource_head(paused):
    store, stale = paused
    fresh = store.load_head(stale.run_id)
    fresh.config["usage"]["calls"] += 3
    store.save_head(fresh)
    recover_review_pauses(store, stale)
    assert store.load_head(stale.run_id).config["usage"]["calls"] == 40


def test_hidden_target_and_changed_reflect_target_do_not_clear_errors(paused):
    store, state = paused
    review_id = f"request-{_identifier('window:d', 3, 'review')}"
    review = store.get(state.run_id, "request", review_id, scope_id="window:d")
    review.pop("version")
    hidden_method = store.get(state.run_id, "method", "method-e", scope_id="window:e")
    review["frame"]["review_targets"] = [{"kind": "method", "object_id": hidden_method["id"],
                                          "version": hidden_method["version"]}]
    store.put(state.run_id, "request", review)
    assert recover_review_pauses(store, state)["recovered_count"] == 4
    d = store.get(state.run_id, "session", f"session-{_identifier('window:d')}", scope_id="window:d")
    assert d["status"] == "error" and "review_debt" not in d


def test_recovery_script_default_dry_run_verifies_copy_and_preserves_original(paused):
    store, state = paused
    before = store.load_head(state.run_id).model_dump()
    sessions = deepcopy(objects(store, state, "session"))
    summary = recover_database(store.path, state.run_id)
    assert summary["status"] == "dry_run" and summary["provider_calls"] == 0
    assert summary["recovered_count"] == 5 and summary["saved_draft_count"] == 3
    assert summary["deferred_reflect_count"] == 2
    assert summary["before_verification"]["kernel_replay"]
    assert summary["after_verification"]["object_integrity"]
    assert store.load_head(state.run_id).model_dump() == before
    assert objects(store, state, "session") == sessions


def test_recovery_script_apply_requires_new_backup_and_preserves_frozen_work(paused, tmp_path):
    store, state = paused
    with pytest.raises(RecoveryFailure) as missing:
        recover_database(store.path, state.run_id, apply=True)
    assert missing.value.code == "BACKUP_REQUIRED"
    backup = tmp_path / "before-recovery.sqlite3"
    summary = recover_database(store.path, state.run_id, apply=True, backup=backup)
    assert summary["status"] == "applied" and summary["recovered_count"] == 5
    assert store.load_head(state.run_id).status == "idle"
    assert backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    original_backup = backup.read_bytes()
    with pytest.raises(RecoveryFailure) as exists:
        recover_database(store.path, state.run_id, apply=True, backup=backup)
    assert exists.value.code == "BACKUP_ALREADY_EXISTS"
    assert backup.read_bytes() == original_backup
    saved = GrowthStore(backup)
    try:
        assert all(actor.status == "error" for actor in saved.load_head(state.run_id).windows.values())
        assert saved.count_objects(state.run_id, "receipt") == 0
    finally:
        saved.close()
