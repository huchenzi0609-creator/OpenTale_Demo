from __future__ import annotations

import pytest
from opentale.growth_capabilities import (
    CapabilityError,
    capability_table,
    collect_capabilities,
    offered_ids,
    resolve_handle,
)


def test_frozen_pages_offer_exact_link_view_and_typed_members_to_both_consumers():
    frame = {"pages": [
        {"kind": "link", "id": "edge", "version": "edge-v", "object": {
            "id": "edge", "version": "edge-v", "source_id": "left", "target_id": "right",
        }},
        {"kind": "view", "id": "view", "version": "view-v", "object": {
            "id": "view", "version": "view-v", "members": [
                {"kind": "node", "object_id": "left", "version": "node-v"}],
        }},
    ]}
    offered = offered_ids(frame)
    assert offered["link"] == offered["edge"] == {"edge"}
    assert offered["view"] == {"view"}
    assert offered["node"] == {"left", "right"}
    assert resolve_handle(frame, "edge").as_dict() == {
        "kind": "link", "object_id": "edge", "version": "edge-v",
    }
    assert resolve_handle(frame, "left").version == "node-v"
    assert resolve_handle(frame, "right").version is None
    frozen = {"capabilities": capability_table(frame)}
    assert collect_capabilities(frozen) == collect_capabilities(frame)


def test_method_organization_outputs_feedback_and_delivered_readings_are_real_evidence():
    frame = {"method_work": {
        "adopted_ref": {"kind": "method", "object_id": "method", "version": "method-v"},
        "candidate_refs": [{"kind": "method", "object_id": "candidate", "version": "candidate-v"}],
        "recent_result_refs": [{"kind": "trial", "object_id": "trial", "version": "trial-v"},
                               {"kind": "passage", "object_id": "reading", "version": "reading-v"}],
        "recent_feedback_refs": [{"kind": "response", "object_id": "feedback", "version": "feedback-v"}],
    }, "view": {"id": "organization", "version": "view-v"},
        "method_dependencies": [{"kind": "method", "object_id": "prior", "version": "prior-v"}]}
    assert {cap.kind for cap in collect_capabilities(frame)} == {"method", "trial", "passage", "response", "view"}
    assert resolve_handle(frame, "reading").kind == "passage"
    assert "reading" not in offered_ids(frame)["node"]


def test_ref_like_strings_and_undelivered_nested_dependencies_grant_no_access():
    frame = {"direction": "private-node", "method": {
        "id": "method", "version": "version", "instruction": "private-node",
        "source_refs": [{"kind": "node", "object_id": "private-node", "version": "secret-v"}],
    }}
    with pytest.raises(CapabilityError, match="^HANDLE_UNKNOWN$"):
        resolve_handle(frame, "private-node")
    assert "private-node" not in str(capability_table(frame))


def test_same_id_multiversion_or_multikind_requires_exact_typed_reference():
    frame = {"methods": [{"id": "same", "version": "old"}],
             "pages": [{"method": {"id": "same", "version": "new"}}],
             "nodes": [{"id": "same", "version": "node-v"}]}
    with pytest.raises(CapabilityError, match="^HANDLE_AMBIGUOUS$"):
        resolve_handle(frame, "same")
    with pytest.raises(CapabilityError, match="^HANDLE_AMBIGUOUS$"):
        resolve_handle(frame, "same", "method")
    assert resolve_handle(frame, "same", "method", "old").version == "old"
    assert resolve_handle(frame, "same", "node").version == "node-v"
    with pytest.raises(CapabilityError, match="^HANDLE_VERSION_MISMATCH$"):
        resolve_handle(frame, "same", "method", "unseen")
    with pytest.raises(CapabilityError, match="^HANDLE_KIND_MISMATCH$"):
        resolve_handle(frame, "same", "trial")


def test_explicit_host_table_cannot_be_expanded_by_unlisted_preview_or_prose():
    frame = {"capabilities": [{"kind": "node", "object_id": "visible", "version": "v"}],
             "nodes": [{"id": "private-node", "version": "secret"}],
             "pages": [{"kind": "node", "id": "private-node", "version": "secret"}]}
    assert offered_ids(frame)["node"] == {"visible"}
    with pytest.raises(CapabilityError):
        resolve_handle(frame, "private-node")


@pytest.mark.parametrize("table", [None, {}, ["private text"], [{"kind": "node"}],
                                   [{"kind": "private", "object_id": "private-node"}],
                                   [{"kind": "node", "object_id": "node", "version": False}],
                                   [{"kind": "node", "object_id": "node", "text": "private text"}]])
def test_malformed_capability_table_is_safe_fixed_diagnostic(table):
    with pytest.raises(CapabilityError) as error:
        collect_capabilities({"capabilities": table})
    assert error.value.issue == "CAPABILITY_TABLE_INVALID"
    assert "private" not in str(error.value)
