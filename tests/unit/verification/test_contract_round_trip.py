from pathlib import Path

import yaml

from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_ROOT = REPO_ROOT / "benchmark" / "vue-fastapi-admin"
CANONICAL_CHANGE = BENCHMARK_ROOT / "eval-fixtures" / "samples" / "eval-sample-001"


def test_canonical_api_plan_satisfies_its_mechanical_contract() -> None:
    plan_texts = {
        f"plans/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((CANONICAL_CHANGE / "plans").glob("api*.md"))
    }
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((CANONICAL_CHANGE / "cases").glob("**/case.yaml"))
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
