from __future__ import annotations

import pytest

from agent_runtime_contracts import AgentRunResult
from graph_engine.plugin_api import TaskActivityReconcileResult, TaskOutcome
from harness import (  # pyright: ignore[reportMissingImports]
    _SECRET_TEXT,
    _bound_fixture,
    _completed_engine_invocation,
    _open_code_fixture,
    _terminal_success_fixture,
    task_request,
)


def _reconcile(result: object) -> TaskActivityReconcileResult:
    assert isinstance(result, TaskActivityReconcileResult)
    return result


@pytest.mark.parametrize(
    "cut",
    ["before_create", "after_create_before_response", "invalid_success_body", "proxy_reset"],
)
async def test_create_cuts_never_issue_a_second_post(cut: str) -> None:
    fixture = _open_code_fixture(create_cut=cut)
    try:
        await fixture.execute_until_cut()
        await fixture.reconcile_twice()
        assert fixture.fake.create_calls <= 1
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "cut",
    ["before_prompt_post", "after_admission_before_response", "after_lost_success_response"],
)
async def test_prompt_cuts_converge_to_one_admission(cut: str) -> None:
    fixture = _bound_fixture(prompt_cut=cut)
    try:
        await fixture.run_and_reconcile()
        assert fixture.fake.accepted_message_count(fixture.reference.expected_message_id) == 1
    finally:
        fixture.close()


async def test_temporary_empty_discovery_stays_indeterminate() -> None:
    fixture = _open_code_fixture(create_cut="after_create_before_response", hide_sessions=True)
    try:
        await fixture.execute_until_cut()
        result = _reconcile(await fixture.reconcile())
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_multiple_matches_fail_closed() -> None:
    fixture = _open_code_fixture(existing_matches=2)
    try:
        result = _reconcile(await fixture.reconcile())
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 0
    finally:
        fixture.close()


async def test_missing_bound_session_is_indeterminate() -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        session_id = fixture.reference.session_id
        assert session_id is not None
        fixture.fake.drop_session(session_id)
        result = _reconcile(await fixture.reconcile())
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "mutator",
    ["foreign", "request_drift"],
)
async def test_foreign_and_drift_fail_closed(mutator: str) -> None:
    fixture = _open_code_fixture()
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        if mutator == "foreign":
            session_id = fixture.reference.session_id
            assert session_id is not None
            fixture.fake.set_session_metadata(session_id, {**fixture.metadata, "activity_id": "foreign"})
            result = _reconcile(await fixture.reconcile())
        else:
            result = _reconcile(
                await fixture.handler.reconcile(task_request(attempt=2), fixture.context, fixture.activity)
            )
        assert result.status == "indeterminate"
        assert fixture.fake.create_calls == 1
    finally:
        fixture.close()


async def test_malformed_get_response_is_indeterminate() -> None:
    fixture = _terminal_success_fixture()
    try:
        session_id = fixture.reference.session_id
        assert session_id is not None
        fixture.fake.fault_on(f"/session/{session_id}/message", "malformed_response")
        result = _reconcile(await fixture.reconcile())
        assert result.status == "indeterminate"
    finally:
        fixture.close()


async def test_oversized_get_response_is_indeterminate() -> None:
    fixture = _terminal_success_fixture()
    try:
        session_id = fixture.reference.session_id
        assert session_id is not None
        fixture.fake.fault_on(f"/session/{session_id}/message", "oversized_response")
        result = _reconcile(await fixture.reconcile())
        assert result.status == "indeterminate"
    finally:
        fixture.close()


async def test_result_schema_failure_is_non_retryable() -> None:
    fixture = _terminal_success_fixture()
    fixture.fake.structured_result = {"ok": True, "tokens": 1}
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.kind == "invalid_output"
        assert outcome.failure.retryable is False
    finally:
        fixture.close()


async def test_fast_terminal_reduces_to_schema_valid_result() -> None:
    fixture = _terminal_success_fixture()
    try:
        result = _reconcile(await fixture.reconcile())
        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        AgentRunResult.model_validate(result.outcome.output)
    finally:
        fixture.close()


async def test_lost_sse_authenticates_with_get() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="gap")
    try:
        result = _reconcile(await fixture.reconcile())
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "terminal"
        assert fixture.fake.count("GET", f"/session/{session_id}") >= 1
    finally:
        fixture.close()


async def test_poll_fallback_reaches_terminal() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="silent",
        request_timeout_seconds=0.2,
    )
    fixture.fake.sse_silent_seconds = 2.0
    try:
        result = _reconcile(await fixture.reconcile())
        assert result.status == "terminal"
        assert fixture.fake.count("GET", "/session/status") >= 1
    finally:
        fixture.close()


async def test_idle_with_open_tools_is_not_terminal() -> None:
    fixture = _bound_fixture(terminal_mode="open_tools")
    try:
        result = _reconcile(await fixture.reconcile())
        assert result.status == "running"
    finally:
        fixture.close()


async def test_completion_cancel_race_provider_terminal_wins() -> None:
    fixture = _terminal_success_fixture()
    try:
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        AgentRunResult.model_validate(result.outcome.output)
    finally:
        fixture.close()


async def test_cancel_cut_is_indeterminate() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        session_id = fixture.reference.session_id
        assert session_id is not None
        fixture.fake.fault_on(f"/session/{session_id}/abort", "disconnect")
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
    finally:
        fixture.close()


async def test_provider_error_is_typed_non_retryable_and_redacted() -> None:
    fixture = _bound_fixture(terminal_mode="error", sse_mode="fast_idle")
    fixture.fake.error_message = f"Authorization: Bearer {_SECRET_TEXT}"
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert isinstance(outcome, TaskOutcome)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.kind == "external_effect"
        assert outcome.failure.retryable is False
        assert _SECRET_TEXT not in outcome.failure.message
        assert outcome.failure.message != "provider error"
    finally:
        fixture.close()


async def test_receipt_before_engine_ack_replays_without_provider() -> None:
    fixture = _terminal_success_fixture()
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "succeeded"
        receipt = outcome
        fixture.fake.reject_all_requests()
        AgentRunResult.model_validate(receipt.output)
        assert receipt.status == "succeeded"
    finally:
        fixture.close()


def test_replay_without_provider() -> None:
    fixture = _completed_engine_invocation()
    try:
        fixture.fake.reject_all_requests()
        assert fixture.reopen_and_run().terminal == "succeeded"
    finally:
        fixture.close()
