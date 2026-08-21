from __future__ import annotations

import pytest

from harness import (  # pyright: ignore[reportMissingImports]
    _bound_fixture,
    prompt_admission_body,
)


@pytest.mark.parametrize(
    "cut",
    ["before_prompt_post", "after_admission_before_response", "after_lost_success_response"],
)
async def test_prompt_crashes_converge_to_one_admission(cut: str) -> None:
    fixture = _bound_fixture(prompt_cut=cut)
    try:
        await fixture.run_and_reconcile()
        assert fixture.fake.accepted_message_count(fixture.reference.expected_message_id) == 1
    finally:
        fixture.close()


async def test_same_message_id_with_changed_body_fails_closed() -> None:
    fixture = _bound_fixture(existing_message_body={"changed": True})
    try:
        result = await fixture.reconcile()
        assert result.status == "indeterminate"
        assert fixture.fake.prompt_posts == 0
    finally:
        fixture.close()


async def test_prompt_body_forwards_exact_selection_without_extras() -> None:
    fixture = _bound_fixture(terminal_mode="success")
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.prompt_posts == 1
        body = fixture.fake.prompt_bodies[0]
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        assert body == expected
        assert set(body) <= {"messageID", "parts", "model", "agent"}
        assert "system" not in body
        assert "tools" not in body
        assert "fallback" not in body
        assert body["agent"] == "fixture-v1"
        assert "model" not in body
    finally:
        fixture.close()
