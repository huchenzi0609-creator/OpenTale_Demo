import asyncio

import pytest
from opentale.domain import Limits
from opentale.kernel import KernelError
from opentale.runner import Runner
from opentale.storage import Store
from opentale.verification import verify_mechanisms


async def test_two_windows_reinterpret_and_recovery_replays(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    first = await runner.step(snapshot.run.id)
    assert first.run.revision == 1
    assert first.run.usage.calls == 7
    assert len(first.story) == 1 and first.pending is None
    assert all(c.resume_count == 1 for c in first.continuations)
    assert all(
        "sources" not in relation and "use_id" not in relation
        for relation in runner.reading_frame(first).open_relations
    )
    second = await runner.step(snapshot.run.id)
    assert second.run.revision == 2 and second.run.usage.calls == 14
    assert second.reading.interpretations[0]["segment_id"] == first.story[0].id
    store.recover()
    assert store.load(snapshot.run.id).model_dump() == second.model_dump()


async def test_budget_prevents_even_first_call(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    snapshot.run.limits = Limits(max_calls=1)
    store.save(snapshot)
    with pytest.raises(KernelError, match="调用额度"):
        await Runner(store, settings).step(snapshot.run.id)
    saved = store.load(snapshot.run.id)
    assert saved.run.status == "budget_exhausted"
    assert saved.run.usage.calls == 0


async def test_counterfactual_detached_and_never_spends_provider_calls(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    installed = await runner.step(snapshot.run.id)
    before = installed.model_dump()
    result = await runner.counterfactual(snapshot.run.id, "writer", "keeper")
    assert result["structural_effect"] and result["semantic_fixture_effect"]
    assert store.load(snapshot.run.id).model_dump() == before


async def test_background_start_pause_is_bounded(snapshot, settings, tmp_path):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    await runner.start(snapshot.run.id)
    runner.pause(snapshot.run.id)
    await asyncio.wait_for(runner.tasks[snapshot.run.id], 3)
    assert store.load(snapshot.run.id).run.status == "paused"
    assert not runner.busy(snapshot.run.id)


async def test_builtin_verification_executes_all_checks(settings):
    result = await verify_mechanisms(settings)
    assert result["ok"] and len(result["checks"]) >= 8
