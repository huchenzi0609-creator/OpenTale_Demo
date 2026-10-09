"""Isolated graph/reading mechanics and kernel replay; never loads provider credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from opentale.config import Settings
from opentale.domain import Limits
from opentale.growth_kernel import GrowthError, add_link, add_node
from opentale.growth_methods import adopt_method, compile_method, revise_method
from opentale.growth_replay import replay_operations, verify_objects
from opentale.growth_runner import GrowthRunner
from opentale.growth_types import MethodVersion, ObjectRef


class VerificationFailure(Exception):
    """Fixed check labels only; material text and transport errors stay private."""


def require(condition, label):
    if not condition:
        raise VerificationFailure(label)


def all_rows(store, run_id, kind, *, scope=None, owner=False, page_size=2):
    """Shared bounded-pagination assertion used by this report and API tests."""
    offset, result, seen = 0, [], set()
    while True:
        page = store.list(run_id, kind, scope_id=scope, include_private=owner,
                          offset=offset, limit=page_size)
        require(not seen.intersection(row["id"] for row in page), "pagination_duplicate")
        seen.update(row["id"] for row in page)
        result.extend(page)
        if len(page) < page_size:
            return result
        offset += len(page)


def verify_privacy(runner, run_id):
    """Actual negative scoped reads and a rejected cross-scope parent selection."""
    store, state = runner.store, runner.load(run_id)
    state = add_node(store, state, "door", "她私下记得一条尚未讲出的路。",
                     private_scope="window:door", request_id="privacy-source")
    private_id = state.windows["door"].working_node_id
    state = add_node(store, state, "door", "那条私下记得的路继续影响她的选择。",
                     parents=[private_id], request_id="privacy-derived")
    derived_id = state.windows["door"].working_node_id
    for node_id in (private_id, derived_id):
        require(store.get(run_id, "node", node_id) is None, "public_private_node_leak")
        require(store.get(run_id, "node", node_id, scope_id="window:stairs") is None, "peer_private_node_leak")
        require(store.get(run_id, "node", node_id, scope_id="window:door") is not None, "private_owner_missing")
    before = state.graph_commit
    try:
        add_node(store, state, "stairs", "另一位置不能接住这条私下的路。", parents=[private_id],
                 request_id="privacy-rejected")
    except GrowthError:
        pass
    else:
        raise VerificationFailure("private_parent_allowed")
    require(runner.load(run_id).graph_commit == before, "rejected_privacy_changed_graph")
    return {"owner_can_read": True, "peer_and_public_cannot_read": True,
            "derived_node_remains_private": True, "cross_scope_parent_rejected": True}


def criteria_loop(runner, run_id, node_ids):
    """Execute a changed evaluation organization, preserving exact evidence refs.

    The assessments are explicit offline fixture boundary inputs. Their quality
    is not measured by invoking the same deterministic compiler that stores them.
    """
    state, store, scope = runner.load(run_id), runner.store, "window:door"
    old = runner.put(run_id, "method", MethodVersion(
        id="verification-criterion", scope_id=scope, instruction="查看两处人物留下的动作。",
        program={"node_ids": node_ids[:1]}, private_scope=scope,
        created_commit=state.graph_commit,
    ).model_dump())
    old_ref = ObjectRef(kind="method", object_id=old["id"], version=old["version"])
    old_view = compile_method(store, state, scope, old_ref, node_ids)
    feedback = runner.put(run_id, "response", {
        "id": "verification-criteria-feedback", "scope_id": scope, "private_scope": scope,
        "text_ref": store.put_text("评议下一次比较两处回应所留下的不同后果。").model_dump(),
        "about_ids": [old["id"]], "evaluation_method_ref": old_ref.model_dump(),
    })
    feedback_ref = ObjectRef(kind="response", object_id=feedback["id"], version=feedback["version"])
    new_ref = revise_method(store, state, scope, old_ref,
        instruction="将两处回应组织为同一关系，比较它们各自留下的后果。",
        program={"node_ids": node_ids, "groups": {"回应比较": node_ids}, "relations": [
            {"source_id": node_ids[0], "target_id": node_ids[1], "relation": "比较不同后果"}]},
        source_refs=[old_ref, feedback_ref])
    new_view = compile_method(store, state, scope, new_ref, node_ids)
    state = adopt_method(store, state, scope, new_ref)
    second = runner.put(run_id, "response", {
        "id": "verification-criteria-reapplication", "scope_id": scope, "private_scope": scope,
        "text_ref": store.put_text("改变后的评议保留两处人物回应的不同方向。").model_dump(),
        "about_ids": [old["id"]], "evaluation_method_ref": new_ref.model_dump(),
        "view_ref": {"kind": "view", "object_id": new_view.id, "version": new_view.version},
    })
    require(old_ref.version != new_ref.version, "criteria_version_unchanged")
    require(len(old_view.members) == 1 and len(new_view.members) == len(node_ids), "criteria_selection_unchanged")
    require(bool(new_view.groups) and bool(new_view.relations), "criteria_structure_missing")
    require(state.windows["door"].method_ref == new_ref, "criteria_adoption_missing")
    require(second["evaluation_method_ref"] == new_ref.model_dump(), "criteria_reapplication_missing")
    return {"old_ref": old_ref.model_dump(), "feedback_ref": feedback_ref.model_dump(),
            "new_ref": new_ref.model_dump(), "applied_view": {"id": new_view.id, "version": new_view.version},
            "changed_selection": True, "groups": len(new_view.groups), "relations": len(new_view.relations),
            "assessment_boundary": "explicit offline fixture text; literary criteria are not judged"}


async def verify(actions: int, reading_steps: int, page_size: int, directory: Path) -> dict:
    settings = Settings()  # Plain defaults: no .env.local or process credentials.
    runner = GrowthRunner(directory, settings)
    try:
        snapshot = runner.create(title="离线生长与遍历验收", provider="fixture", windows=[
            {"id": "door", "label": "门前", "seed": "她把手放在门闩上。", "direction": "接住门前的回应"},
            {"id": "stairs", "label": "台阶", "seed": "他在台阶下等着。", "direction": "继续另一个方向"},
        ], shared_seed="门前有一杯温水。", limits=Limits(max_calls=None, max_tokens=None, max_windows=None))
        run_id, store = snapshot["run"]["id"], runner.store
        for _ in range(actions):
            snapshot = await runner.step(run_id)
        require(snapshot["run"]["provider"] == "fixture", "fixture_provider_required")
        chains = []
        for scope in ("window:door", "window:stairs"):
            receipts = all_rows(store, run_id, "receipt", scope=scope, page_size=page_size)
            require([row["action"] for row in receipts[:9]] == [
                "inspect", "apply", "try_write", "respond", "revise", "adopt", "apply", "try_write", "publish",
            ], "method_lifecycle_missing")
            trials = all_rows(store, run_id, "trial", scope=scope, page_size=page_size)
            feedback = all_rows(store, run_id, "response", scope=scope, page_size=page_size)
            require(trials[0]["method_ref"] != trials[1]["method_ref"], "new_method_not_used")
            require(trials[0]["feedback_refs"] and trials[1]["adopted"], "trial_feedback_publish_missing")
            revised = store.get(run_id, "method", trials[1]["method_ref"]["object_id"],
                                version=trials[1]["method_ref"]["version"], scope_id=scope)
            require(any(ref["kind"] == "response" and ref["object_id"] == feedback[0]["id"]
                        for ref in revised["source_refs"]), "revision_feedback_evidence_missing")
            chains.append({"scope_id": scope, "old_method": trials[0]["method_ref"],
                           "old_trial": trials[0]["id"], "feedback": trials[0]["feedback_refs"],
                           "new_method": trials[1]["method_ref"], "new_trial": trials[1]["id"], "published": True})
        state = runner.load(run_id)
        parents = [state.windows[key].working_node_id for key in ("door", "stairs")]
        state = add_node(store, state, "door", "两个人在台阶前各自接住杯子，把两条路带到了同一处。",
                         parents=parents, request_id="verification-join")
        joined = state.windows["door"].working_node_id
        shared = state.windows["stairs"].entry_node_ids[-1]
        state = add_link(store, state, joined, shared, scope_id="window:door", request_id="association-return")
        state = add_link(store, state, shared, joined, scope_id="window:stairs", request_id="association-outward")
        links = all_rows(store, run_id, "link", owner=True, page_size=page_size)
        require(len([link for link in links if link["kind"] == "growth" and link["target_id"] == joined]) == 2,
                "multiple_parent_join_missing")
        require(any(link["source_id"] == joined and link["target_id"] == shared for link in links)
                and any(link["source_id"] == shared and link["target_id"] == joined for link in links),
                "association_cycle_missing")
        criteria = criteria_loop(runner, run_id, parents)
        privacy = verify_privacy(runner, run_id)
        state = runner.load(run_id)
        track_ids = []
        for key in ("door", "stairs"):
            snapshot = runner.add_reading(run_id, key + "读轨", [state.windows[key].entry_node_ids[0]])
            track_ids.append(next(track["id"] for track in snapshot["readings"] if track["title"] == key + "读轨"))
        frozen_commit = runner.load(run_id).graph_commit
        readings = []
        public_node_ids = {row["id"] for row in all_rows(store, run_id, "node", page_size=page_size)}
        for track_id in track_ids:
            for _ in range(reading_steps):
                await runner.step(run_id, track_id)
            visits = all_rows(store, run_id, "visit", scope="reading:" + track_id, page_size=page_size)
            passages = all_rows(store, run_id, "passage", scope="reading:" + track_id, page_size=page_size)
            require(len(visits) >= 2 and len(passages) >= 2, "same_graph_multiple_reads_missing")
            require(all(visit["track_id"] == track_id for visit in visits), "reader_visit_leak")
            require(all(visit["observed_graph_commit"] == frozen_commit for visit in visits), "reader_horizon_changed")
            require(bool(public_node_ids - {visit["node_id"] for visit in visits}), "incomplete_reading_missing")
            for passage in passages:
                require(all(any(visit["id"] == key for visit in visits) for key in passage["visit_ids"]), "passage_visit_missing")
            readings.append({"track_id": track_id, "read_seq": runner.load(run_id).reading_tracks[track_id].read_seq,
                             "visits": len(visits), "passages": len(passages), "observed_graph_commit": frozen_commit,
                             "unvisited_nodes": len(public_node_ids - {visit["node_id"] for visit in visits})})
        require(runner.load(run_id).graph_commit == frozen_commit, "reading_changed_world")
        visits_a = all_rows(store, run_id, "visit", scope="reading:" + track_ids[0], page_size=page_size)
        visits_b = all_rows(store, run_id, "visit", scope="reading:" + track_ids[1], page_size=page_size)
        require(visits_a[0]["node_id"] != visits_b[0]["node_id"], "independent_entrances_missing")
        require(store.list(run_id, "visit") == [] and store.list(run_id, "passage") == [], "public_reader_record_leak")
        counts = {kind: len(all_rows(store, run_id, kind, owner=True, page_size=page_size))
                  for kind in ("node", "link", "method", "trial", "response", "receipt", "request")}
        require(counts["node"] == store.count_objects(run_id, "node"), "indexed_node_pagination_incomplete")
        integrity_cursor, integrity_count, integrity_pages = 0, 0, 0
        while True:
            checked = verify_objects(store, run_id, after_seq=integrity_cursor, limit=page_size)
            integrity_count += checked["objects_checked"]
            integrity_pages += 1
            integrity_cursor = checked["through_seq"]
            if not checked["has_more"]:
                break
        integrity = {"level": "object_integrity", "run_id": run_id, "after_seq": 0,
                     "through_seq": integrity_cursor, "objects_checked": integrity_count,
                     "pages_checked": integrity_pages, "has_more": False,
                     "full_object_history": True, "semantic_replay": False}
        replay = replay_operations(store, run_id, page_size=page_size)
        require(replay["current_head_matches"] and replay["operational_replay"], "operational_replay_failed")
        usage = runner.load(run_id).config["usage"]
        require(usage["input_tokens"] == usage["output_tokens"] == usage["reserved_tokens"] == 0, "fixture_usage_invalid")
        return {"ok": True, "provider": "fixture", "schema_version": 6, "method_language_version": 3,
                "growth_actions": actions, "reading_actions_per_track": reading_steps,
                "graph_commit": frozen_commit, "counts": counts, "usage": usage,
                "topology": {"multiple_parent_join": True, "shared_entry_branches": True, "association_cycle": True},
                "method_chains": chains, "criteria_change": criteria, "readings": readings, "privacy": privacy,
                "pagination": {"page_size": page_size, "unique_object_heads": True, "terminates": True},
                "object_integrity": integrity, "replay": replay,
                "limitations": ["Offline fixtures demonstrate execution and provenance, not LLM intention or literary quality.",
                                "Kernel replay uses frozen provider outputs and work controls as boundary inputs.",
                                "Semantic/literary replay is not claimed; no paid model calls were made."]}
    finally:
        await runner.shutdown()
        runner.store.close()


def bounded_int(minimum, maximum):
    def parse(value):
        integer = int(value)
        if not minimum <= integer <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return integer
    return parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=bounded_int(18, 1000), default=22)
    parser.add_argument("--reading-steps", type=bounded_int(6, 9), default=9)
    parser.add_argument("--page-size", type=bounded_int(1, 100), default=2)
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="opentale-growth-verification-") as temporary:
            report = asyncio.run(verify(args.actions, args.reading_steps, args.page_size, Path(temporary).resolve()))
    except VerificationFailure as error:
        print(json.dumps({"ok": False, "check": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"ok": False, "check": "isolated_growth_verification_failed"}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
