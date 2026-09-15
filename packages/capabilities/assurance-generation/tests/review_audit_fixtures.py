from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_runtime_contracts import AgentRunRequest
from graph_engine.frozen_json import thaw_json
from assurance_generation.operations.review import review_prepare_handler
from tests.product.test_change_local_output_routing import execute_task
from codegen_fixtures import codegen_result, locked_oracle_paths  # pyright: ignore[reportMissingImports]
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    plan_input,
    review_result,
)

HELPER = "capabilities.domain_factories.item.rows"
HELPER_PATH = "qa/tests/testdata/domain/item.py"
PLAN_PATH = "qa/results/codegen/api-codegen-summary.md"
GENERATED_TEST = "qa/tests/api/items/test_items.py"


def write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_review(root: Path, review: dict[str, Any]) -> None:
    write(root, "qa/results/review/api-codegen-review.json", json.dumps(review))
    write(root / "qa/.staging/attempt-1", "qa/results/review/api-codegen-review.json", json.dumps(review))


def write_codegen_artifacts(
    root: Path,
    family: str = "api",
    *,
    summary: str | None = None,
    helper_mention: str | None = None,
    generated_target: bool = True,
) -> dict[str, Any]:
    test_path, data_path = locked_oracle_paths(family)
    manifest = codegen_result(files=[test_path, data_path], family=family)
    body = summary
    if body is None:
        mention = helper_mention or ""
        target_line = f"- `{HELPER_PATH}`: implement rows.\n" if generated_target else "None.\n"
        body = f"# Codegen\n\n## Import Strategy\n{mention}\n\n## Target Files\n{target_line}"
    write(root, f"qa/results/codegen/{family}-codegen-summary.md", body)
    write(root, f"qa/results/codegen/{family}-generated-files.json", json.dumps(manifest))
    write(root, data_path, "helper\n")
    return manifest


def review_prepare_input(family: str, business: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(business or plan_input(family))
    test_path, data_path = locked_oracle_paths(family)
    payload["codegen_output"] = codegen_result(files=[test_path, data_path], family=family)
    return payload


async def audited_review(
    root: Path, *, helper: bool = False, source: str | None = None, generated_target: bool = True
) -> tuple[dict[str, Any], dict[str, Any], AgentRunRequest]:
    business = plan_input("api")
    write(root, "qa/proposal.md", "# Proposal\n")
    write(root, "qa/cases/items/case.yaml", json.dumps(business["reviewed_cases"]))
    helper_mention = ""
    if helper:
        business["capability_leafs"] = [*business["capability_leafs"], HELPER]
        write(
            root,
            ".aa/data-knowledge.yaml",
            "capabilities:\n  domain_factories:\n    item:\n      rows:\n"
            "        kind: helper\n        symbol: tests.testdata.domain.item.rows\n",
        )
        helper_mention = f"Consume `{HELPER}` and `tests.testdata.domain.item.rows`."
        if source is not None:
            write(root, HELPER_PATH, source)
    write_codegen_artifacts(
        root,
        "api",
        helper_mention=helper_mention,
        generated_target=generated_target,
    )
    write(root, GENERATED_TEST, "def test_tc_api_001__happy_path():\n    assert True\n")
    if helper:
        write(
            root,
            "qa/results/plans/api-codegen-mapping.json",
            json.dumps(
                {
                    "schema_version": "1",
                    "layer": "api",
                    "entries": [
                        {
                            "case_id": "TC_API_001",
                            "symbol": "test_tc_api_001__happy_path",
                            "target_file": HELPER_PATH if generated_target else GENERATED_TEST,
                        }
                    ],
                }
            ),
        )
    prepared = await execute_task(
        review_prepare_handler("api"),
        review_prepare_input("api", business),
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
                "evidence_paths": [PLAN_PATH],
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
