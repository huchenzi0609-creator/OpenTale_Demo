"""Versioned execution, exact request recovery, source slots and durable object checkpoints."""

import json

import pytest
from conftest import ROOT
from fastapi.testclient import TestClient
from opentale.api import create_app
from opentale.domain import Limits, Operation, Proposal
from opentale.kernel import KernelError, fingerprint, install_batch, semantic_projection
from opentale.presets import initialize, read_presets
from opentale.providers import FixtureProvider, ProviderError
from opentale.reflection import position_scope
from opentale.replay import verify_replay
from opentale.request_evidence import verify_request_evidence
from opentale.request_types import RequestEnvelope
from opentale.runner import Runner
from opentale.storage import StorageError, Store


def new_state(mode="reflexive_v2"):
    preset = next(p for p in read_presets(ROOT) if p["id"] == "letters")
    return initialize(preset, "fixture", Limits(max_calls=None, max_tokens=None, max_windows=None), mode)


async def test_v2_window_exact_requests_and_replay(tmp_path, settings):
    state = new_state()
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    for _ in range(2):
        state = await runner.step(state.run.id)
        assert verify_replay(state, store.read_events(state.run.id))["ok"]
    assert state.schema_version == 5
    assert len(state.requests) == 14
    assert all(request.accepted_result_hash and request.attempts for request in state.requests)
    assert verify_request_evidence(state)
    assert all(request.evidence_level == "exact_projection" for request in state.requests)
    assert all(request.attempts[0].projection for request in state.requests)
    assert state.schedule.mode == "shadow"
    assert len(state.schedule.jobs) == 7
    assert state.reflection.views and state.reflection.effects and state.reflection.responses
    assert state.run.usage.calls == 14
    assert state.run.usage.input_tokens == state.run.usage.output_tokens == 0
    clone = state.model_copy(deep=True)
    clone.requests[0].frame["name"] = "tampered"
    assert not verify_request_evidence(clone)
    clone = state.model_copy(deep=True)
    accepted = next(event for event in clone.events if event.type == "continuation_resumed")
    accepted.data["surface"] += "修改后的文本"
    assert not verify_request_evidence(clone)


@pytest.mark.parametrize("mode", ["reflexive", "reflexive_v2"])
async def test_partial_proposal_retry_uses_original_frozen_inputs(tmp_path, settings, monkeypatch, mode):
    state = new_state(mode)
    store = Store(tmp_path)
    store.save(state)
    runner = Runner(store, settings)
    # First form an installed method, then interrupt a later proposal window.
    state = await runner.step(state.run.id)

    class FailOnce(FixtureProvider):
        failed = False

        async def propose(self, frame, **kwargs):
            if frame.position_id == "keeper" and not self.failed:
                self.failed = True
                raise ProviderError("MODEL_TIMEOUT", "fixture", True)
            return await super().propose(frame, **kwargs)

    provider = FailOnce()
    monkeypatch.setattr("opentale.runner.make_provider", lambda *_: provider)
    with pytest.raises(ProviderError):
        await runner.step(state.run.id)
    failed = store.load(state.run.id)
    original = failed.pending.proposal_frames["writer"].model_copy(deep=True)
    assert "writer" in failed.pending.proposals
    scope = position_scope("writer")
    method = failed.reflection.adoptions[scope][0]
    runner.release_method(state.run.id, scope, method)
    completed = await runner.step(state.run.id)
    choice = next(choice for choice in completed.reflection.choices
                  if choice.scope_id == scope and choice.stage == "propose" and choice.revision == 2)
    assert choice.input_fingerprint == original.reflection.input_fingerprint
    assert choice.methods == original.reflection.active_methods
    assert verify_replay(completed, store.read_events(state.run.id))["ok"]
    if mode == "reflexive_v2":
        requests = [request for request in completed.requests
                    if request.stage == "propose" and request.input_world_revision == 1]
        assert len(requests) == 3
        assert sorted(len(request.attempts) for request in requests) == [1, 1, 2]
        assert verify_request_evidence(completed)


def proposal(state, source, target, expression, text, op="rebind"):
    return Proposal(position_id=source, based_on_revision=state.run.revision, text=text,
                    operations=[Operation(op=op, target_position_id=target, expression_id=expression,
                                          role="来信", value=text, anchor=text)])


def test_sparse_source_slots_preserve_unscheduled_writer_and_explicit_withdraw():
    state = new_state()
    expression = next(use.expression_id for use in state.uses if use.role != "主体背景")
    first = install_batch(state, [
        proposal(state, "writer", "keeper", expression, "第一人的解释"),
        proposal(state, "reader", "keeper", expression, "第二人的解释"),
    ], 0, participants={"writer", "reader"})
    changed = install_batch(first, [proposal(first, "writer", "keeper", expression, "第一人的新解释")],
                            1, participants={"writer"})
    use = next(use for use in changed.uses if use.position_id == "keeper" and use.role == "来信")
    assert {binding.value for binding in use.bindings} == {"第一人的新解释", "第二人的解释"}
    removed = install_batch(changed, [proposal(changed, "reader", "keeper", expression, "撤回那句话", "withdraw")],
                            2, participants={"reader"})
    use = next(use for use in removed.uses if use.position_id == "keeper" and use.role == "来信")
    assert [binding.value for binding in use.bindings] == ["第一人的新解释"]
    assert semantic_projection(state) != semantic_projection(first)


def test_legacy_withdraw_rejected_and_conflicting_slot_is_atomic():
    state = new_state("reflexive")
    expression = next(use.expression_id for use in state.uses if use.role != "主体背景")
    drafts = [proposal(state, position.id, "keeper", expression, "撤回", "withdraw") for position in state.positions]
    with pytest.raises(KernelError, match="来源槽位"):
        install_batch(state, drafts, 0)
    state = new_state()
    draft = proposal(state, "writer", "keeper", expression, "撤回也更新")
    draft.operations.append(draft.operations[0].model_copy(update={"op": "withdraw"}))
    before = state.model_dump()
    with pytest.raises(KernelError) as error:
        install_batch(state, [draft], 0, participants={"writer"})
    assert error.value.code == "BINDING_SLOT_CONFLICT" and state.model_dump() == before


def test_content_addressed_checkpoint_dedup_fork_and_corruption(tmp_path):
    state = new_state()
    store = Store(tmp_path)
    store.save(state)
    assert store.load(state.run.id).model_dump() == state.model_dump()
    with store.connect() as db:
        checkpoint = json.loads(db.execute("SELECT snapshot FROM runs").fetchone()[0])
        assert checkpoint["storage_format"] == "opentale-checkpoint-v2"
        count = db.execute("SELECT count(*) FROM objects").fetchone()[0]
    fork = store.fork(state.run.id)
    store.delete(state.run.id)
    assert store.load(fork.run.id).positions == state.positions
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM objects").fetchone()[0] < count + 20
        root = json.loads(db.execute("SELECT snapshot FROM runs").fetchone()[0])["root"]["$node"]
        db.execute("UPDATE objects SET body=? WHERE id=?", ('{"kind":"text","value":"changed"}', root))
    with pytest.raises(StorageError):
        store.load(fork.run.id)


def test_content_dedup_does_not_alias_mutable_history(tmp_path):
    from opentale.domain import Event

    state = new_state()
    state.audit["sample"] = {"items": []}
    state.events.append(Event(id="alias-test", type="diagnostic", revision=0, data={"items": []}))
    store = Store(tmp_path)
    store.save(state)
    restored = store.load(state.run.id)
    restored.audit["sample"]["items"].append("changed")
    assert restored.events[-1].data == {"items": []}
    assert store.load(state.run.id).audit["sample"] == {"items": []}


def test_accepted_request_without_received_attempt_is_rejected():
    state = new_state()
    state.requests.append(RequestEnvelope(
        id="request-fake", epoch_id="fake", stage="propose", scope_id="position:writer",
        input_world_revision=0, policy_revision=0, frame={"revision": 0}, frame_hash=fingerprint({"revision": 0}),
        request_hash="fake", projection_version="test", evidence_level="exact_projection", accepted_result_hash="fake",
    ))
    assert not verify_request_evidence(state)


async def test_manual_reading_recovers_feedback_after_saved_prose(tmp_path, settings, monkeypatch):
    store = Store(tmp_path)
    state = new_state()
    store.save(state)
    runner = Runner(store, settings)
    state = await runner.step(state.run.id)
    state = runner.add_reading(state.run.id, "另一入口", "keeper", enabled=False)
    reading_id = state.reading_tracks[-1].id
    original = runner._install_reading_feedback
    failed = False

    def interrupt_once(snapshot, entries):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("offline interruption after prose checkpoint")
        return original(snapshot, entries)

    monkeypatch.setattr(runner, "_install_reading_feedback", interrupt_once)
    with pytest.raises(ProviderError):
        await runner.present_reading(state.run.id, reading_id)
    partial = store.load(state.run.id)
    calls = partial.run.usage.calls
    assert partial.reading_tracks[-1].story
    recovered = await runner.present_reading(state.run.id, reading_id)
    assert recovered.run.usage.calls == calls
    assert any(entry["reading_id"] == reading_id for event in recovered.events
               if event.type == "reflection_feedback" for entry in event.data["entries"])
    assert verify_replay(recovered, store.read_events(state.run.id))["ok"]
    assert verify_request_evidence(recovered)


def test_v2_api_full_material_permissions_and_pair_without_calls(tmp_path):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        response = client.post("/api/runs", json={"preset_id": "letters", "provider": "fixture",
                                                "mode": "reflexive_v2", "limits": {"max_calls": None}})
        assert response.status_code == 200
        state = response.json()
        path = f"/api/runs/{state['run']['id']}"
        catalog = client.get(path + "/objects", params={"scope_id": "position:writer"}).json()
        assert catalog["objects"]
        own = next(row for row in catalog["objects"] if row.get("role") == "主体背景")
        assert client.get(path + "/object", params={"scope_id": "position:writer", "object_id": own["id"]}).status_code == 200
        assert client.get(path + "/object", params={"scope_id": "position:reader", "object_id": own["id"]}).status_code == 422
        assert client.get(path + "/objects", params={"scope_id": "position:missing"}).status_code == 422
        state = client.post(path + "/step").json()
        assert "frame" not in state["requests"][0]
        assert "projection" not in state["requests"][0]["attempts"][0]
        exported = client.get(path + "/export").json()
        assert exported["requests"][0]["frame"]
        scope, methods = next((key, value) for key, value in state["reflection"]["adoptions"].items() if value)
        paired = client.post(path + "/experiments", json={"scope_id": scope, "method_id": methods[0]})
        assert paired.status_code == 200
        pair = paired.json()
        assert pair["control"]["run"]["usage"]["calls"] == pair["intervention"]["run"]["usage"]["calls"] == 0
        assert methods[0] in pair["control"]["reflection"]["adoptions"][scope]
        assert methods[0] not in pair["intervention"]["reflection"]["adoptions"][scope]
        assert client.get(path).json() == state
        for arm in ("control", "intervention"):
            branch = pair[arm]["run"]["id"]
            assert client.get(f"/api/runs/{branch}/replay").json()["ok"]


def test_paired_experiment_blocks_changed_execution_before_model_call(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, tmp_path / "data")) as client:
        state = client.post("/api/runs", json={"preset_id": "letters", "mode": "reflexive_v2"}).json()
        path = f"/api/runs/{state['run']['id']}"
        state = client.post(path + "/step").json()
        scope, methods = next((key, value) for key, value in state["reflection"]["adoptions"].items() if value)
        pair = client.post(path + "/experiments", json={"scope_id": scope, "method_id": methods[0]}).json()
        branch = f"/api/runs/{pair['control']['run']['id']}"
        monkeypatch.setattr("opentale.runner.execution_signature", lambda *_: "changed-implementation")
        result = client.post(branch + "/step")
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "EXPERIMENT_CONFIGURATION_CHANGED"
        assert client.get(branch).json()["run"]["usage"]["calls"] == 0
