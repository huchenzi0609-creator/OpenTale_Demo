"""Manual presentation remains visible, exclusive and safe without evolving actors."""

import asyncio
import traceback

import opentale.runner as runner_module
import pytest
from opentale.kernel import KernelError
from opentale.providers import FixtureProvider, ProviderError
from opentale.runner import Runner
from opentale.storage import Store


async def test_manual_presentation_is_visible_and_excludes_concurrent_world_edits(snapshot, settings, tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    completed = await runner.step(snapshot.run.id)
    added = runner.add_reading(completed.run.id, "另一个入口", "keeper")
    reading_id = added.reading_tracks[-1].id
    entered, release = asyncio.Event(), asyncio.Event()
    fixture = FixtureProvider()

    async def delayed(frame):
        entered.set()
        await release.wait()
        return await FixtureProvider().present(frame)

    monkeypatch.setattr(fixture, "present", delayed)
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: fixture)
    task = asyncio.create_task(runner.present_reading(completed.run.id, reading_id))
    await asyncio.wait_for(entered.wait(), 2)
    assert store.load(completed.run.id).run.status == "running"
    assert runner.busy(completed.run.id)
    with pytest.raises(KernelError, match="暂停"):
        runner.add_reading(completed.run.id, "不能并发修改", "writer")
    release.set()
    result = await asyncio.wait_for(task, 2)
    assert result.run.status == completed.run.status and result.run.phase == completed.run.phase
    assert result.run.usage.windows == completed.run.usage.windows
    assert result.continuations == completed.continuations


async def test_manual_unexpected_failure_is_sanitized_and_restores_state(snapshot, settings, tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save(snapshot)
    runner = Runner(store, settings)
    completed = await runner.step(snapshot.run.id)
    added = runner.add_reading(completed.run.id, "会失败的读法", "keeper")
    reading_id = added.reading_tracks[-1].id
    fixture = FixtureProvider()

    async def unexpected(_):
        raise ValueError("raw-private-response mock-only-credential")

    monkeypatch.setattr(fixture, "present", unexpected)
    monkeypatch.setattr(runner_module, "make_provider", lambda *_: fixture)
    with pytest.raises(ProviderError) as caught:
        await runner.present_reading(completed.run.id, reading_id)
    assert caught.value.code == "RUNTIME_FAILURE"
    visible = "".join(traceback.format_exception(caught.value))
    assert "raw-private-response" not in visible and "mock-only-credential" not in visible
    saved = store.load(completed.run.id)
    assert saved.run.status == completed.run.status
    assert saved.run.usage.failed_calls == 1
    assert "raw-private-response" not in saved.model_dump_json()
