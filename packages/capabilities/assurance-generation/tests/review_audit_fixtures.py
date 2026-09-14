from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_runtime_contracts import AgentRunRequest
from graph_engine.frozen_json import thaw_json
from assurance_generation.operations.review import review_prepare_handler
from tests.product.test_change_local_output_routing import execute_task
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    family_plan_files,
    plan_input,
    review_result,
    valid_plan_result,
)

HELPER = "capabilities.domain_factories.item.rows"
HELPER_PATH = "qa/tests/testdata/domain/item.py"
PLAN_PATH = "qa/results/plans/api-codegen-plan.md"


def write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_review(root: Path, review: dict[str, Any]) -> None:
    write(root, "qa/results/review/api-plan-review.json", json.dumps(review))
    write(root / "qa/.staging/attempt-1", "qa/results/review/api-plan-review.json", json.dumps(review))


async def audited_review(
    root: Path, *, helper: bool = False, source: str | None = None, generated_target: bool = True
) -> tuple[dict[str, Any], dict[str, Any], AgentRunRequest]:
    business = plan_input("api")
    for path in family_plan_files("api"):
        write(root, path, "# Plan\n")
    write(root, "qa/proposal.md", "# Proposal\n")
    write(root, "qa/cases/items/case.yaml", json.dumps(business["reviewed_cases"]))
    if helper:
        business["capability_leafs"] = [*business["capability_leafs"], HELPER]
        write(
            root,
            ".aa/data-knowledge.yaml",
            "capabilities:\n  domain_factories:\n    item:\n      rows:\n"
            "        kind: helper\n        symbol: tests.testdata.domain.item.rows\n",
        )
        write(
            root,
            PLAN_PATH,
            "# Codegen\n\n## Import Strategy\n"
            f"Consume `{HELPER}` and `tests.testdata.domain.item.rows`.\n\n"
            "## Target Files\n"
            + (f"- `{HELPER_PATH}`: implement rows.\n" if generated_target else "None.\n"),
        )
        if source is not None:
            write(root, HELPER_PATH, source)
    prepared = await execute_task(
        review_prepare_handler("api"),
        {**business, "reviewed_plan": valid_plan_result("api")},
        root,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    instructions = [thaw_json(part.json_content) for part in request.instructions]
    requirements = next(
        part["review_requirements"]
        for part in instructions
        if isinstance(part, dict) and "review_requirements" in part
    )
    review = review_result("api")
    audit = {
        "input_refs": requirements["input_refs"],
        "planning_facts_digest": requirements["planning_facts_digest"],
        "cases": [
            {
                "case_id": case_id,
                "checks": {
                    area: "pass" for area in ("request", "auth", "setup", "assertion", "cleanup", "helpers")
                },
                "evidence_paths": ["qa/results/plans/api-plan.md"],
                "finding_ids": [],
                "rationale": "Request, setup, oracle and cleanup agree.",
            }
            for case_id in requirements["case_ids"]
        ],
        "helpers": [],
    }
    for observed in requirements["helpers"]:
        existing = observed["observed_signature"] is not None and not observed["is_stub"]
        row = {key: value for key, value in observed.items() if key != "is_stub"}
        row.update(
            implementation="existing" if existing else "planned",
            invocation="async" if observed["observed_async"] else "sync",
            plan_location=None if existing else {"artifact": PLAN_PATH, "section": "Target Files"},
            evidence_paths=[".aa/data-knowledge.yaml", HELPER_PATH if existing else PLAN_PATH],
            finding_ids=[],
            rationale="The source or bounded generation target provides this helper.",
        )
        audit["helpers"].append(row)
    review["review_audit"] = audit
    write_review(root, review)
    return review, business, request
