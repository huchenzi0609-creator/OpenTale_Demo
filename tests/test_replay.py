import json

from opentale.replay import verify_replay
from opentale.runner import Runner
from opentale.storage import Store


async def test_complete_semantic_replay_two_windows_and_fork(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    assert verify_replay(snapshot, store.read_events(snapshot.run.id))["ok"]
    await runner.step(snapshot.run.id)
    saved = await runner.step(snapshot.run.id)
    result = verify_replay(saved, store.read_events(saved.run.id))
    assert result["ok"] and len(result["checks"]) == 6
    fork = store.fork(saved.run.id)
    advanced = await runner.step(fork.run.id)
    assert verify_replay(advanced, store.read_events(fork.run.id))["ok"]
    assert verify_replay(store.load(saved.run.id), store.read_events(saved.run.id)) == result


async def test_replay_detects_tampered_or_missing_execution_evidence(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    store.save(snapshot)
    saved = await Runner(store, settings).step(snapshot.run.id)
    events = store.read_events(saved.run.id)
    assert not verify_replay(saved, [e for e in events if e.type != "batch_installed"])["ok"]
    target = next(e for e in events if e.type == "continuation_resumed")
    target.data["condition_fingerprint"] = "changed"
    assert not verify_replay(saved, events)["ok"]
    # Corrupted SQLite event evidence must not silently fall back to embedded snapshot evidence.
    with store.connect() as db:
        event = next(e for e in events if e.type == "batch_installed")
        db.execute(
            "UPDATE events SET event=? WHERE run_id=? AND id=?",
            (json.dumps({**event.model_dump(), "data": {}}), saved.run.id, event.id),
        )
    assert not verify_replay(saved, store.read_events(saved.run.id))["ok"]
