from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_opencode.discovery import OpenCodeActivityReference, OpenCodeSessionCreateRequest
from agent_runtime_opencode.protocol import OpenCodeHttpClient, canonical_json_text
from harness import (  # pyright: ignore[reportMissingImports]
    _CANARY,
    _SECRET_TEXT,
    _open_code_fixture,
    metadata_with_discovery,
    task_request,
)


@pytest.mark.parametrize(
    "cut",
    ["before_create", "after_create_before_response", "invalid_success_body", "proxy_reset"],
)
async def test_ambiguous_create_never_posts_twice(cut: str) -> None:
    fixture = _open_code_fixture(create_cut=cut)
    try:
        await fixture.execute_until_cut()
        await fixture.reconcile_twice()
        assert fixture.fake.count("POST", "/session") <= 1
    finally:
        fixture.close()


async def test_fresh_execute_posts_one_metadata_create_without_caller_id() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.create_calls == 1
        body = fixture.fake.create_bodies[0]
        assert "id" not in body
        assert "parentID" not in body
        assert body["metadata"] == fixture.metadata
        assert _SECRET_TEXT not in canonical_json_text(body)
        snapshot = fixture.port.snapshot
        assert snapshot.state == "bound"
        reference = OpenCodeActivityReference.model_validate(snapshot.reference)
        assert reference.session_id is not None
        assert not reference.session_id.startswith("ses_caller")
        assert fixture.fake.generated_ids == (reference.session_id,)
    finally:
        fixture.close()


async def test_empty_list_after_ambiguous_create_stays_indeterminate() -> None:
    fixture = _open_code_fixture(create_cut="after_create_before_response", hide_sessions=True)
    try:
        await fixture.execute_until_cut()
        first = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert first.status == "indeterminate"
        assert first.status != "absent"
        second = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert second.status == "indeterminate"
        assert second.status != "absent"
        assert fixture.fake.create_calls == 1
        assert second.reason is not None
    finally:
        fixture.close()


async def test_ambiguous_create_rediscovers_exactly_one_later_match() -> None:
    fixture = _open_code_fixture(create_cut="after_create_before_response")
    try:
        await fixture.execute_until_cut()
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "running"
        assert fixture.fake.create_calls == 1
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.session_id in fixture.fake.generated_ids
    finally:
        fixture.close()


async def test_formerly_bound_404_is_missing_not_create_authorization() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.session_id is not None
        fixture.fake.drop_session(reference.session_id)
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert result.status != "absent"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_foreign_metadata_after_bind_is_rejected() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.session_id is not None
        fixture.fake.set_session_metadata(
            reference.session_id,
            metadata_with_discovery(fixture.metadata, activity_id="foreign"),
        )
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_parent_id_reconnect_is_rejected_when_bound_session_is_missing() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.session_id is not None
        fixture.fake.drop_session(reference.session_id)
        fixture.fake.add_session(
            session_id="ses_child",
            metadata=fixture.metadata,
            parent_id=reference.session_id,
        )
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
        assert fixture.port.snapshot.reference is not None
        bound = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert bound.session_id != "ses_child"
    finally:
        fixture.close()


async def test_changed_request_identity_is_rejected() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        drifted = task_request(attempt=2)
        result = await fixture.handler.reconcile(drifted, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_changed_message_identity_is_rejected() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        fixture.port.replace_bound_reference(
            {**reference.model_dump(mode="json"), "expected_message_id": "0" * 64}
        )
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_execute_creates_a_child_of_the_exact_run_root() -> None:
    fixture = _open_code_fixture(config_overrides={"parent_session_id": "ses_root"})
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        body = fixture.fake.create_bodies[0]
        assert body["parentID"] == "ses_root"
        assert "id" not in body
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.parent_session_id == "ses_root"
        assert reference.worktree == fixture.fake.project_scope
    finally:
        fixture.close()


async def test_wrong_parent_and_wrong_worktree_are_not_adopted() -> None:
    fixture = _open_code_fixture(config_overrides={"parent_session_id": "ses_root"})
    try:
        fixture.fake.add_session(
            session_id="ses_wrong_parent",
            metadata=fixture.metadata,
            parent_id="ses_other",
        )
        fixture.fake.add_session(
            session_id="ses_wrong_tree",
            metadata=fixture.metadata,
            parent_id="ses_root",
            directory="/other-worktree",
        )
        await fixture.handler.execute(fixture.request, fixture.context)
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.session_id not in {"ses_wrong_parent", "ses_wrong_tree"}
        assert fixture.fake.create_bodies[0]["parentID"] == "ses_root"
    finally:
        fixture.close()


async def test_duplicate_children_stay_indeterminate() -> None:
    fixture = _open_code_fixture(config_overrides={"parent_session_id": "ses_root"})
    try:
        fixture.fake.add_session(session_id="ses_a", metadata=fixture.metadata, parent_id="ses_root")
        fixture.fake.add_session(session_id="ses_b", metadata=fixture.metadata, parent_id="ses_root")
        result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert result.reason == "multiple exact metadata matches"
        assert fixture.fake.create_calls == 0
    finally:
        fixture.close()


async def test_response_lost_child_is_adopted_on_reconcile() -> None:
    fixture = _open_code_fixture(
        create_cut="after_create_before_response",
        config_overrides={"parent_session_id": "ses_root"},
    )
    try:
        await fixture.execute_until_cut()
        result = await fixture.reconcile()
        assert result.status == "running"
        reference = OpenCodeActivityReference.model_validate(fixture.port.snapshot.reference)
        assert reference.parent_session_id == "ses_root"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_create_session_client_rejects_id_and_accepts_parent_id() -> None:
    fixture = _open_code_fixture()
    try:
        client = OpenCodeHttpClient(fixture.config, secret=_CANARY)
        try:
            with pytest.raises(ValidationError):
                await client.create_session({"title": "x", "metadata": fixture.metadata, "id": "ses_caller"})
            created = await client.create_session(
                {"title": "x", "metadata": fixture.metadata, "parentID": "ses_parent"}
            )
            assert created["parentID"] == "ses_parent"
        finally:
            await client.aclose()
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


def test_typed_create_request_model_exists() -> None:
    assert "id" not in OpenCodeSessionCreateRequest.model_fields
    assert "parentID" in OpenCodeSessionCreateRequest.model_fields
    assert "parent_id" not in OpenCodeSessionCreateRequest.model_fields
