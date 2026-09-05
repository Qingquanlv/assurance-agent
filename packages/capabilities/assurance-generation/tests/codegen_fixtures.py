from __future__ import annotations

from typing import Any

from graph_engine.plugin_api import CandidateFile, CandidateWriteSet, ResourceClaims, ValidationContext

from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    family_case_id,
    fake_agent_result,
    plan_input,
    reviewed_cases,
    valid_plan_result,
)

_SHA = "a" * 64
FAMILY_TEST_ROOTS = {
    "api": "tests/api",
    "e2e": "tests/e2e",
    "fuzz": "tests/fuzz",
    "performance": "tests/perf",
}
FAMILY_TEST_FILES = {
    "api": "tests/api/test_users.py",
    "e2e": "tests/e2e/test_users.py",
    "fuzz": "tests/fuzz/test_users.py",
    "performance": "tests/perf/test_users.py",
}


def family_test_file(family: str) -> str:
    return FAMILY_TEST_FILES[family]


def staged_generated_file(
    family: str,
    target: str | None = None,
    *,
    change_id: str = "CH-DEMO-001",
) -> str:
    return f"qa/changes/{change_id}/generated/{family}/files/{target or family_test_file(family)}"


def family_symbol(family: str) -> str:
    return f"test_{family_case_id(family).lower()}__happy_path"


def mapping_document(family: str, *, target_file: str | None = None) -> dict[str, Any]:
    path = target_file or family_test_file(family)
    return {
        "schema_version": "1",
        "layer": family,
        "entries": [
            {
                "case_id": family_case_id(family),
                "symbol": family_symbol(family),
                "target_file": path,
            }
        ],
    }


def codegen_result(
    files: list[str],
    *,
    family: str = "api",
    required_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    case_id = family_case_id(family)
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "layer": family,
        "files": [
            {
                "repo_path": path,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": [case_id],
            }
            for path in files
        ],
        "mapping": mapping_document(family, target_file=files[0] if files else family_test_file(family)),
        "required_capabilities": required_capabilities or ["entities.item.create"],
    }


def codegen_input(family: str) -> dict[str, Any]:
    payload = plan_input(family)
    return {
        "change_id": payload["change_id"],
        "plan_digest": payload["plan_digest"],
        "plan_ref": payload["plan_ref"],
        "capability_leafs": payload["capability_leafs"],
        "reviewed_plan": valid_plan_result(family),
        "reviewed_cases": reviewed_cases(family),
        "family_constraints": payload["family_constraints"],
        "baseline_tree_id": "0" * 64,
    }


def codegen_fix_input(family: str, *, allowed_paths: list[str] | None = None) -> dict[str, Any]:
    payload = codegen_input(family)
    paths = allowed_paths or [family_test_file(family)]
    payload["allowed_paths"] = paths
    payload["approved_proposal"] = {
        "proposal_id": f"FIX-{family.upper()}-001",
        "status": "approved",
        "files_to_modify": paths,
    }
    return payload


def generated_candidate(family: str, extra_file: str | None = None) -> CandidateWriteSet:
    if family not in FAMILY_TEST_FILES:
        raise ValueError(f"unknown generation family: {family}")
    listed = [extra_file] if extra_file is not None else [FAMILY_TEST_FILES[family]]
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in listed),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


__all__ = [
    "FAMILIES",
    "FAMILY_TEST_FILES",
    "FAMILY_TEST_ROOTS",
    "VALID_LEAFS",
    "candidate_with",
    "codegen_fix_input",
    "codegen_input",
    "codegen_result",
    "family_case_id",
    "family_symbol",
    "family_test_file",
    "staged_generated_file",
    "fake_agent_result",
    "generated_candidate",
    "mapping_document",
    "validation_context",
]
