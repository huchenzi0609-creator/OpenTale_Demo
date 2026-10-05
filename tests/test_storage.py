from opentale.domain import Limits, Pending
from opentale.kernel import frame_for
from opentale.providers import FixtureProvider
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


async def test_recovery_fork_preserves_pending_and_can_use_a_smaller_parallel_limit(snapshot, tmp_path):
    provider = FixtureProvider()
    position_id = snapshot.positions[0].id
    proposal = (await provider.propose(frame_for(snapshot, position_id))).value
    snapshot.pending = Pending(base_revision=0, proposals={position_id: proposal})
    snapshot.run.status = "error"
    snapshot.run.phase = "drafting"
    snapshot.run.usage.calls = 3
    snapshot.run.usage.unknown_tokens = 20000
    store = Store(tmp_path / "store")
    store.save(snapshot)
    before = store.load(snapshot.run.id).model_dump()
    limits = Limits(max_calls=16, max_tokens=120000, max_parallel=1, max_windows=1)
    fork = store.fork(snapshot.run.id, "恢复分支", limits=limits)
    assert fork.pending.model_dump() == before["pending"]
    assert fork.run.limits == limits
    assert fork.run.usage.calls == fork.run.usage.unknown_tokens == 0
    assert fork.positions == snapshot.positions and fork.uses == snapshot.uses
    limits.max_calls = 20
    assert fork.run.limits.max_calls == 16
    assert store.load(snapshot.run.id).model_dump() == before
