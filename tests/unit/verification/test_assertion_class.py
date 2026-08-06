"""API and E2E assertion-strength classifiers (§5-B2) — reusable for M2."""

from __future__ import annotations

from assurance_agent.verification.assertion_class import (
    classify_api_assertions,
    classify_e2e_assertions,
    counts_as_covered_oracle,
)


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_api_constant_assert_is_weak() -> None:
    source = "def test_x():\n    assert True\n"
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert result.surface == "api"
    assert "constant" in " ".join(result.reasons)
    assert counts_as_covered_oracle(result) is False


def test_api_constant_comparison_is_weak() -> None:
    source = "def test_x():\n    assert 1 == 1\n"
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert counts_as_covered_oracle(result) is False


def test_api_helper_only_is_weak() -> None:
    source = (
        "def test_x(client):\n"
        "    response = client.post('/x')\n"
        "    assert_ok(response)\n"
        "    check_json_shape(response)\n"
    )
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert "helper_only" in result.reasons
    assert counts_as_covered_oracle(result) is False


def test_api_status_200_only_is_weak() -> None:
    source = (
        "def test_x(client):\n    response = client.get('/menus')\n    assert response.status_code == 200\n"
    )
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert "status_200_only" in result.reasons
    assert counts_as_covered_oracle(result) is False


def test_api_business_predicate_is_strong() -> None:
    source = (
        "def test_x(client):\n"
        "    response = client.post('/depts', json={'name': 'dup'})\n"
        "    assert response.status_code == 400\n"
        "    body = response.json()\n"
        "    assert body['detail'] == 'name already exists'\n"
    )
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "strong"
    assert counts_as_covered_oracle(result) is True


def test_api_status_200_plus_business_field_is_strong() -> None:
    """A 200 check does not poison a co-located business predicate."""
    source = (
        "def test_x(client):\n"
        "    response = client.get('/depts/1')\n"
        "    assert response.status_code == 200\n"
        "    assert response.json()['name'] == 'Engineering'\n"
    )
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "strong"
    assert counts_as_covered_oracle(result) is True


def test_api_response_ok_assert_is_weak_not_covered_oracle() -> None:
    """§5-B2: unknown/unlisted shapes are fail-closed weak, not strong."""
    source = "def test_x(client):\n    response = client.get('/menus')\n    assert response.ok\n"
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert counts_as_covered_oracle(result) is False


def test_api_bare_name_assert_is_weak_not_covered_oracle() -> None:
    source = "def test_x(x):\n    assert x\n"
    result = classify_api_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert counts_as_covered_oracle(result) is False


def test_api_class_scoped_test_method_is_classified() -> None:
    source = (
        "class TestDeptApi:\n"
        "    def test_duplicate(self, client):\n"
        "        response = client.post('/depts', json={'name': 'dup'})\n"
        "        assert response.status_code == 400\n"
        "        assert response.json()['detail'] == 'name already exists'\n"
    )
    result = classify_api_assertions(source, function_name="test_duplicate")
    assert result.strength == "strong"
    assert counts_as_covered_oracle(result) is True


# ---------------------------------------------------------------------------
# E2E surface
# ---------------------------------------------------------------------------


def test_e2e_visibility_only_is_weak() -> None:
    source = (
        "async def test_x(page):\n"
        "    await page.goto('/apis')\n"
        "    await expect(page.get_by_role('heading')).to_be_visible()\n"
        "    assert await page.locator('.table').is_visible()\n"
    )
    result = classify_e2e_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert result.surface == "e2e"
    assert any(
        r in ("visibility_only", "page_load_only") for r in result.reasons
    ) or "visibility" in " ".join(result.reasons)
    assert counts_as_covered_oracle(result) is False


def test_e2e_page_load_only_is_weak() -> None:
    source = (
        "async def test_x(page):\n"
        "    await page.goto('/apis')\n"
        "    await page.wait_for_load_state('networkidle')\n"
    )
    result = classify_e2e_assertions(source, function_name="test_x")
    assert result.strength == "weak"
    assert counts_as_covered_oracle(result) is False


def test_e2e_cross_page_postcondition_is_strong() -> None:
    source = (
        "async def test_x(page):\n"
        "    await page.goto('/apis/new')\n"
        "    await page.fill('#name', 'billing')\n"
        "    await page.click('button[type=submit]')\n"
        "    await page.goto('/apis')\n"
        "    await expect(page.get_by_text('billing')).to_be_visible()\n"
        "    assert await page.get_by_text('billing').count() == 1\n"
    )
    result = classify_e2e_assertions(source, function_name="test_x")
    assert result.strength == "strong"
    assert counts_as_covered_oracle(result) is True


def test_e2e_business_text_postcondition_is_strong() -> None:
    source = (
        "async def test_x(page):\n"
        "    await page.goto('/apis')\n"
        "    await expect(page.get_by_test_id('api-row')).to_contain_text('billing')\n"
    )
    result = classify_e2e_assertions(source, function_name="test_x")
    assert result.strength == "strong"
    assert counts_as_covered_oracle(result) is True


def test_weak_oracle_handoff_contract_for_coverage_join() -> None:
    """Task 6 joins B2 into covered; this is the reusable gate it must call.

    There is no sufficiency/coverage hook yet that consumes assertion strength
    (``evaluate_sufficiency`` is case-centric; constraint covered join is Task 6).
    The classifier's ``counts_as_covered_oracle`` is the sealed handoff: weak
    never counts, strong does.
    """
    weak = classify_api_assertions("def t():\n    assert response.status_code == 200\n", function_name="t")
    strong = classify_api_assertions(
        "def t():\n    assert response.json()['code'] == 'DUPLICATE'\n",
        function_name="t",
    )
    assert counts_as_covered_oracle(weak) is False
    assert counts_as_covered_oracle(strong) is True
