from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_ROOT = REPO_ROOT / "benchmark" / "vue-fastapi-admin"
CANONICAL_API_CHANGE = BENCHMARK_ROOT / "eval-fixtures" / "samples" / "eval-sample-001"
E2E_CONTRACT_ROOT = REPO_ROOT / "tests" / "fixtures" / "assurance" / "e2e-contract"
E2E_CHANGE_ID = "CH-E2E-CONTRACT-001"


def _load_e2e_contract_bundle() -> tuple[
    dict[str, str], list[dict[str, object]], dict[str, object], tuple[str, ...]
]:
    plan_texts = {
        f"plans/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((E2E_CONTRACT_ROOT / "plans").glob("e2e*.md"))
    }
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((E2E_CONTRACT_ROOT / "cases").glob("**/case.yaml"))
    ]
    data_knowledge = yaml.safe_load(
        (E2E_CONTRACT_ROOT / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8")
    )
    review = yaml.safe_load((E2E_CONTRACT_ROOT / "review" / "plan-review.json").read_text(encoding="utf-8"))
    required_capabilities = tuple(review["required_capabilities"])
    return plan_texts, cases, data_knowledge, required_capabilities


def _e2e_context(
    *,
    plan_texts: dict[str, str] | None = None,
    required_capabilities: tuple[str, ...] | None = None,
) -> CheckContext:
    base_plans, cases, data_knowledge, base_caps = _load_e2e_contract_bundle()
    return CheckContext(
        plan_texts=base_plans if plan_texts is None else plan_texts,
        cases=cases,
        data_knowledge=data_knowledge,
        layer="e2e",
        required_capabilities=base_caps if required_capabilities is None else required_capabilities,
    )


def _check(document, check_id: str):
    return next(item for item in document.checks if item.check_id == check_id)


def test_canonical_api_plan_satisfies_its_mechanical_contract() -> None:
    plan_texts = {
        f"plans/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((CANONICAL_API_CHANGE / "plans").glob("api*.md"))
    }
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((CANONICAL_API_CHANGE / "cases").glob("**/case.yaml"))
    ]
    data_knowledge = yaml.safe_load(
        (BENCHMARK_ROOT / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8")
    )

    document = run_plan_checks(
        CheckContext(
            plan_texts=plan_texts,
            cases=cases,
            data_knowledge=data_knowledge,
            layer="api",
        )
    )

    assert document.status == "pass", document.model_dump(mode="json")
    assert all(not check.findings for check in document.checks)
    assert document.schema_version == "2"
    assert document.layer == "api"
    assert document.applicability is not None
    assert document.applicability.applicable is True
    assert tuple(item.check_id for item in document.checks) == PLAN_CHECK_IDS


def test_canonical_e2e_plan_satisfies_its_mechanical_contract() -> None:
    document = run_plan_checks(_e2e_context())

    assert document.status == "pass", document.model_dump(mode="json")
    assert all(not check.findings for check in document.checks)
    assert document.schema_version == "2"
    assert document.layer == "e2e"
    assert document.applicability is not None
    assert document.applicability.applicable is True
    assert tuple(item.check_id for item in document.checks) == PLAN_CHECK_IDS


@pytest.mark.parametrize(
    ("mutation", "check_id"),
    [
        (
            lambda plans: plans.__setitem__(
                "plans/e2e-codegen-plan.md",
                plans["plans/e2e-codegen-plan.md"].split("## Factory Mapping", 1)[0],
            ),
            "shared_factory",
        ),
        (
            lambda plans: plans.__setitem__(
                "plans/e2e-codegen-plan.md",
                plans["plans/e2e-codegen-plan.md"].replace("| reuse |", "| rewrite |", 1),
            ),
            "shared_factory",
        ),
        (
            lambda plans: plans.__setitem__(
                "plans/e2e-test-data-plan.md",
                plans["plans/e2e-test-data-plan.md"].replace(
                    ".aa/data-knowledge.yaml", "qa/changes/.aa/data-knowledge.yaml", 1
                ),
            ),
            "l1_path",
        ),
        (
            lambda plans: plans.__setitem__(
                "plans/e2e-plan.md",
                plans["plans/e2e-plan.md"].replace(
                    "| TC_E2E_AUTH_REJECT | assert_ideal HTTP 4xx; no operable CRUD |\n",
                    "prose interrupts the Case ID table\n| TC_E2E_AUTH_REJECT | assert_ideal HTTP 4xx; no operable CRUD |\n",
                    1,
                ),
            ),
            "assert_ideal",
        ),
        (
            lambda plans: [
                plans.__setitem__(
                    rel,
                    plans[rel].replace("assert_ideal HTTP 4xx", "HTTP 200 only"),
                )
                for rel in list(plans)
            ],
            "assert_ideal",
        ),
    ],
)
def test_e2e_contract_mutations_fail_the_named_check(mutation, check_id: str) -> None:
    plans, _, _, caps = _load_e2e_contract_bundle()
    mutated = deepcopy(plans)
    mutation(mutated)

    document = run_plan_checks(
        CheckContext(
            plan_texts=mutated,
            cases=_e2e_context().cases,
            data_knowledge=_e2e_context().data_knowledge,
            layer="e2e",
            required_capabilities=caps,
        )
    )

    assert _check(document, check_id).status == "fail", document.model_dump(mode="json")


def test_e2e_contract_missing_capability_fails_capability_keys() -> None:
    _, _, _, caps = _load_e2e_contract_bundle()
    missing_caps = (*caps, "capabilities.adapters.e2e.auth.missing_leaf")

    document = run_plan_checks(_e2e_context(required_capabilities=missing_caps))

    assert _check(document, "capability_keys").status == "fail"
