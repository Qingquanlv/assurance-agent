from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_opencode.discovery import OpenCodeActivityReference, OpenCodeSessionCreateRequest
from agent_runtime_opencode.protocol import OpenCodeHttpClient, canonical_json_text
from harness import (  # pyright: ignore[reportMissingImports]
    _CANARY,
    _SECRET_TEXT,
    _open_code_fixture,
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
            reference.session_id, {**fixture.metadata, "activity_id": "foreign"}
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


async def test_create_session_client_rejects_id_and_parent_id() -> None:
    fixture = _open_code_fixture()
    try:
        client = OpenCodeHttpClient(fixture.config, secret=_CANARY)
        try:
            with pytest.raises(ValidationError):
                await client.create_session({"title": "x", "metadata": fixture.metadata, "id": "ses_caller"})
            with pytest.raises(ValidationError):
                await client.create_session(
                    {"title": "x", "metadata": fixture.metadata, "parentID": "ses_parent"}
                )
        finally:
            await client.aclose()
        assert fixture.fake.create_calls == 0
    finally:
        fixture.close()


def test_typed_create_request_model_exists() -> None:
    assert "id" not in OpenCodeSessionCreateRequest.model_fields
    assert "parentID" not in OpenCodeSessionCreateRequest.model_fields
    assert "parent_id" not in OpenCodeSessionCreateRequest.model_fields
