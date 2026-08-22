from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import CandidateFile, CandidateWriteSet, ResourceClaims, ValidationContext

CHANGE_ID = "CH-DEMO-001"
BATCH_ID = "20260822T000000Z"
LEAF = "entities.item.create"
CASE_ID = "TC_A"
HEX_A = "a" * 64
HEX_B = "b" * 64
EVIDENCE_REF = f"sha256:{HEX_A}"
POLICY_DIGEST = HEX_B
_WHEEL_ROOT = Path(__file__).resolve().parent.parent


def as_object(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def digest_of(value: object) -> str:
    return canonical_digest(cast(JSONValue, value))


def catalog_leafs() -> list[str]:
    return [LEAF, "auth.session.create"]


def trace_input(
    *,
    closed_mapping: list[str],
    observed: list[str],
    change_id: str = CHANGE_ID,
    batch_id: str = BATCH_ID,
    capability_leafs: list[str] | None = None,
    case_id: str = CASE_ID,
) -> dict[str, object]:
    return {
        "change_id": change_id,
        "batch_id": batch_id,
        "phase": "execution",
        "closed_mapping": closed_mapping,
        "observed": observed,
        "capability_leafs": capability_leafs or catalog_leafs(),
        "case_ids": [case_id],
        "cases": [
            {
                "case_id": case_id,
                "module": "menus",
                "case_type": "API",
                "automation_required": True,
                "capability": LEAF,
            }
        ],
    }


def issue_input(*, message: str, change_id: str = CHANGE_ID, batch_id: str = BATCH_ID) -> dict[str, object]:
    return {
        "change_id": change_id,
        "batch_id": batch_id,
        "kind": "test_failure",
        "target": "api",
        "case_id": CASE_ID,
        "source_artifact": "execution/api-result.json",
        "source_json_pointer": "/results/0/message",
        "evidence_refs": [EVIDENCE_REF],
        "signature": message,
        "observed_at": "2026-08-22T00:00:00Z",
    }


def write_set(*paths: str, digest: str = HEX_A) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id=HEX_B,
        candidate_tree_id=HEX_A,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=digest) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def imported_module_names(root: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def production_import_roots() -> set[str]:
    return imported_module_names(_WHEEL_ROOT / "assurance_quality")
