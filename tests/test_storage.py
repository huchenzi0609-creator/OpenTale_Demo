from opentale.storage import Store


def test_idle_recovery_and_fork_preserve_original(snapshot, tmp_path):
    store = Store(tmp_path / "store")
    store.save(snapshot)
    original = store.load(snapshot.run.id).model_dump()
    store.recover()
    assert store.load(snapshot.run.id).model_dump() == original
    fork = store.fork(snapshot.run.id)
    assert fork.run.id != snapshot.run.id
    assert fork.run.fork_of == snapshot.run.id
    fork.positions[0].name = "changed"
    store.save(fork)
    assert store.load(snapshot.run.id).model_dump() == original
    store.delete(fork.run.id)
    assert len(store.list()) == 1


def test_interrupted_reservations_are_conservatively_accounted(snapshot, tmp_path):
    store = Store(tmp_path / "store")
    snapshot.run.status = "running"
    snapshot.run.usage.reserved_tokens = 900
    store.save(snapshot)
    store.recover()
    state = store.load(snapshot.run.id)
    assert state.run.status == "paused"
    assert state.run.usage.reserved_tokens == 0
    assert state.run.usage.unknown_tokens == 900
    before = state.model_dump()
    store.recover()
    assert store.load(snapshot.run.id).model_dump() == before
