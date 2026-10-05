"""Replay semantic operations from recorded evidence without running any provider."""

from .domain import Event, Presentation, Proposal, Snapshot
from .kernel import fingerprint, frame_for, install_batch, install_presentation, semantic_projection


def replay(events: list[Event]) -> Snapshot:
    initial = next(event for event in events if event.type == "created")
    state = Snapshot.model_validate(initial.data["initial"])
    for event in events:
        if event.type == "batch_installed":
            proposals = [Proposal.model_validate(p) for p in event.data["proposals"]]
            state = install_batch(state, proposals, event.data["base_revision"])
        elif event.type == "continuation_resumed":
            continuation = next(c for c in state.continuations if c.position_id == event.data["position_id"])
            frame = frame_for(state, continuation.position_id)
            if frame.condition_fingerprint != event.data["condition_fingerprint"]:
                raise ValueError("Continuation evidence does not match active input")
            if frame.revision != event.data["frame_revision"]:
                raise ValueError("Continuation evidence is stale")
            continuation.surface = event.data["surface"]
            continuation.status = "ready"
            continuation.resume_count += 1
        elif event.type == "presented":
            install_presentation(state, Presentation.model_validate(event.data["presentation"]))
    return state


def verify_replay(snapshot: Snapshot, events: list[Event]) -> dict:
    try:
        reconstructed = replay(events)
        expected, actual = semantic_projection(snapshot), semantic_projection(reconstructed)
        checks = [{"name": key, "passed": expected[key] == actual[key]} for key in expected]
        return {
            "ok": all(c["passed"] for c in checks),
            "digest": fingerprint(actual),
            "checks": checks,
            "note": "重新执行已保存的关系操作，比较表达、用法、续延、正文与读法；不重放外部API或计费。",
        }
    except Exception:
        return {
            "ok": False,
            "digest": None,
            "checks": [{"name": "完整可执行证据", "passed": False}],
            "note": "事件记录缺失、过时或损坏，不能证明重放一致；原存档未修改。",
        }
