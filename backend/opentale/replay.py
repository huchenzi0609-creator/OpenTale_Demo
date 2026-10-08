"""Replay semantic operations from recorded evidence without running any provider."""

from .domain import Event, GenerationFrame, Presentation, Proposal, ReadingFrame, ReadingTrack, Snapshot
from .kernel import (
    fingerprint,
    frame_for,
    install_batch,
    install_presentation,
    reading_frame_for,
    reading_track_for,
    semantic_projection,
)
from .reflection import capture_choice, install_feedback, position_scope, reading_scope
from .reflection_types import ChoiceRecord, ReflectionFrame


def replay(events: list[Event]) -> Snapshot:
    initial = next(event for event in events if event.type == "created")
    state = Snapshot.model_validate(initial.data["initial"])
    actor_frames: dict[str, GenerationFrame] = {}
    proposal_frames: dict[str, GenerationFrame] = {}
    reader_frames: dict[str, ReadingFrame] = {}
    accepted_readings: dict[str, dict] = {}
    for event in events:
        if event.type == "reading_created":
            track = ReadingTrack.model_validate(event.data["track"])
            if any(existing.id == track.id for existing in state.reading_tracks):
                raise ValueError("Duplicate reading creation evidence")
            state.reading_tracks.append(track)
        elif event.type == "reading_enabled":
            reading_track_for(state, event.data["reading_id"]).enabled = event.data["enabled"]
        elif event.type == "proposal_inputs_frozen":
            for key, value in event.data["frames"].items():
                frame = GenerationFrame.model_validate(value)
                if frame != frame_for(state, key):
                    raise ValueError("Proposal inputs do not match freeze-time state")
                proposal_frames[key] = frame
        elif event.type == "request_frozen":
            if fingerprint(event.data["frame"]) != event.data["frame_hash"]:
                raise ValueError("Request evidence hash mismatch")
            if event.data["stage"] == "present":
                reading_id = event.data["scope_id"].removeprefix("reading:")
                frame = ReadingFrame.model_validate(event.data["frame"])
                expected = reader_frames.get(reading_id) or reading_frame_for(state, reading_id)
                if frame != expected:
                    raise ValueError("Request input differs from frozen reader input")
                reader_frames[reading_id] = frame
        elif event.type == "batch_installed":
            proposals = [Proposal.model_validate(p) for p in event.data["proposals"]]
            frozen = None
            if "proposal_frames" in event.data:
                frozen = {key: GenerationFrame.model_validate(value)
                          for key, value in event.data["proposal_frames"].items()}
                if frozen != proposal_frames:
                    raise ValueError("Installed proposals differ from original frozen inputs")
            state = install_batch(state, proposals, event.data["base_revision"], proposal_frames=frozen)
            proposal_frames = {}
            actor_frames = {}
            reader_frames = {}
            accepted_readings = {}
            if state.reflection is not None:
                actor_frames = {key: GenerationFrame.model_validate(value)
                                for key, value in event.data["realization_frames"].items()}
                if set(actor_frames) != {p.id for p in state.positions}:
                    raise ValueError("Missing frozen actor inputs")
                for key, frame in actor_frames.items():
                    if frame != frame_for(state, key):
                        raise ValueError("Frozen actor inputs do not match method execution")
        elif event.type == "reading_inputs_frozen":
            for key, value in event.data["frames"].items():
                frame = ReadingFrame.model_validate(value)
                if frame != reading_frame_for(state, key):
                    raise ValueError("Frozen reading inputs do not match method execution")
                reader_frames[key] = frame
        elif event.type == "continuation_resumed":
            continuation = next(c for c in state.continuations if c.position_id == event.data["position_id"])
            frame = actor_frames.get(continuation.position_id) or frame_for(state, continuation.position_id)
            if frame.condition_fingerprint != event.data["condition_fingerprint"]:
                raise ValueError("Continuation evidence does not match active input")
            if frame.revision != event.data["frame_revision"]:
                raise ValueError("Continuation evidence is stale")
            continuation.surface = event.data["surface"]
            continuation.status = "ready"
            continuation.resume_count += 1
            if state.reflection is not None:
                actual = capture_choice(state, position_scope(continuation.position_id), "realize", frame.reflection)
                if actual != ChoiceRecord.model_validate(event.data["choice"]):
                    raise ValueError("Choice evidence does not match actual input")
        elif event.type == "presented":
            reading_id = event.data.get("reading_id", "main")
            if state.reflection is not None:
                frame = reader_frames.get(reading_id) or reading_frame_for(state, reading_id)
                if frame.reflection != ReflectionFrame.model_validate(event.data["reflection_frame"]):
                    raise ValueError("Reading choice evidence does not match frozen input")
            install_presentation(
                state, Presentation.model_validate(event.data["presentation"]),
                reading_id,
            )
            if state.reflection is not None:
                capture_choice(state, reading_scope(reading_id), "present", frame.reflection)
                accepted_readings[reading_id] = event.data
        elif event.type == "reflection_feedback":
            entries = event.data["entries"]
            for entry in entries:
                accepted = accepted_readings.get(entry["reading_id"])
                if accepted is None or any(entry[key] != accepted[key]
                                           for key in ("presentation", "reflection_frame")):
                    raise ValueError("Feedback lacks accepted reading evidence")
            install_feedback(state, [
                (reading_scope(entry["reading_id"]), Presentation.model_validate(entry["presentation"]).reflection,
                 ReflectionFrame.model_validate(entry["reflection_frame"])) for entry in entries
            ])
        elif event.type == "method_released":
            if state.reflection is None:
                raise ValueError("Method release in classic evidence")
            state.reflection.adoptions[event.data["scope_id"]].remove(event.data["method_id"])
            if state.run.mode == "reflexive_v2":
                state.policy_revision += 1
    return state


def verify_replay(snapshot: Snapshot, events: list[Event]) -> dict:
    try:
        reconstructed = replay(events)
        expected, actual = semantic_projection(snapshot), semantic_projection(reconstructed)
        checks = [{"name": key, "passed": expected[key] == actual[key]} for key in expected]
        if snapshot.run.mode == "reflexive_v2":
            from .request_evidence import verify_request_evidence

            checks.append({"name": "冻结请求与逐次尝试", "passed": verify_request_evidence(snapshot)})
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
