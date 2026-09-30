from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_opencode.session.discovery import (
    OpenCodeActivityReference,
    exact_metadata_matches,
)
from agent_runtime_opencode.transport.http import (
    OpenCodeDiscoveryMetadata,
    OpenCodeSessionCreateRequest,
    canonical_json_text,
)
from harness import (  # pyright: ignore[reportMissingImports]
    _SECRET_TEXT,
    _open_code_fixture,
    metadata_payload,
    reference_payload,
)


def test_discovery_metadata_is_frozen_versioned_and_secret_free() -> None:
    metadata = OpenCodeDiscoveryMetadata.model_validate(metadata_payload())
    assert metadata.schema_version == "1"
    dumped = canonical_json_text(metadata.model_dump(mode="json"))
    assert "secret" not in dumped
    assert _SECRET_TEXT not in dumped
    with pytest.raises(ValidationError, match="frozen"):
        metadata.attempt = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        OpenCodeDiscoveryMetadata.model_validate(metadata_payload(secret=_SECRET_TEXT))


def test_activity_reference_is_opaque_and_allows_unknown_session_id() -> None:
    reference = OpenCodeActivityReference.model_validate(reference_payload(session_id=None))
    assert reference.session_id is None
    dumped = canonical_json_text(reference.model_dump(mode="json"))
    assert "secret" not in dumped
    assert _SECRET_TEXT not in dumped
    with pytest.raises(ValidationError, match="frozen"):
        reference.session_id = "ses_caller"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        OpenCodeActivityReference.model_validate(reference_payload(authorization="Bearer x"))


def _session_metadata(discovery: OpenCodeDiscoveryMetadata) -> dict[str, object]:
    return {
        "discovery": discovery.model_dump(mode="json"),
        "workspace_binding": {"schema_version": "2"},
    }


def test_create_body_forbids_caller_selected_id_and_accepts_exact_parent() -> None:
    metadata = OpenCodeDiscoveryMetadata.model_validate(metadata_payload())
    base = {
        "title": "Assurance · activity · task-1 · #1",
        "metadata": _session_metadata(metadata),
    }
    OpenCodeSessionCreateRequest.model_validate(base)
    child = OpenCodeSessionCreateRequest.model_validate({**base, "parentID": "ses_root"})
    assert child.parentID == "ses_root"
    with pytest.raises(ValidationError):
        OpenCodeSessionCreateRequest.model_validate({**base, "id": "ses_caller"})


def test_exact_metadata_matches_ignore_title_and_exclude_parent_id_children() -> None:
    expected = OpenCodeDiscoveryMetadata.model_validate(metadata_payload())
    foreign = OpenCodeDiscoveryMetadata.model_validate(metadata_payload(activity_id="other"))
    sessions = (
        {"id": "ses_1", "title": "other-title", "metadata": _session_metadata(expected)},
        {"id": "ses_2", "title": "aa:activity-1", "metadata": _session_metadata(foreign)},
        {
            "id": "ses_child",
            "parentID": "ses_1",
            "metadata": _session_metadata(expected),
        },
        {"id": "ses_flat", "metadata": expected.model_dump(mode="json")},
    )
    matches = exact_metadata_matches(sessions, expected)
    assert [item["id"] for item in matches] == ["ses_1"]
    adopted = exact_metadata_matches(sessions, expected, parent_session_id="ses_1", worktree=None)
    assert [item["id"] for item in adopted] == ["ses_child"]
    rejected = exact_metadata_matches(sessions, expected, parent_session_id="ses_other")
    assert rejected == ()
    wrong_tree = exact_metadata_matches(
        ({**sessions[2], "directory": "/other"},),
        expected,
        parent_session_id="ses_1",
        worktree="/work",
    )
    assert wrong_tree == ()


async def test_multiple_exact_metadata_matches_fail_closed() -> None:
    fixture = _open_code_fixture(existing_matches=2)
    try:
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 0
    finally:
        fixture.close()


def test_metadata_match_digest_is_canonical() -> None:
    from agent_runtime_contracts.schema import canonical_digest

    first = OpenCodeDiscoveryMetadata.model_validate(metadata_payload())
    reordered = OpenCodeDiscoveryMetadata.model_validate(
        {
            "adapter_source_digest": first.adapter_source_digest,
            "activity_id": first.activity_id,
            "attempt": first.attempt,
            "invocation_id": first.invocation_id,
            "request_digest": first.request_digest,
            "schema_version": first.schema_version,
            "task_id": first.task_id,
            "activation_id": first.activation_id,
            "workspace_identity_digest": first.workspace_identity_digest,
        }
    )
    assert canonical_digest(first.model_dump(mode="json")) == canonical_digest(
        reordered.model_dump(mode="json")
    )
    assert canonical_digest(first.model_dump(mode="json")) != canonical_digest(
        OpenCodeDiscoveryMetadata.model_validate(metadata_payload(attempt=2)).model_dump(mode="json")
    )
