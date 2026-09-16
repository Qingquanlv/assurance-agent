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


def issue_candidate_document(*, possible_problem_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "evidence_bundle_digest": EVIDENCE_REF,
        "candidates": [
            {
                "candidate_id": "CAND-1",
                "observation_ids": ["OBS-1"],
                "proposed": {
                    "title": "boom",
                    "classification": "product_bug",
                    "severity": "high",
                    "root_cause_hypothesis": "x",
                },
                "affected_surface": {"kind": "module", "value": "Menus Service"},
                "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                "possible_problem_ids": possible_problem_ids,
                "confidence": 0.8,
                "recommended_action": "triage",
            }
        ],
    }


def occurrence_detected_event() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "seq": 1,
        "event_id": "EVT-1",
        "idempotency_key": "k1",
        "ts": "2026-08-22T00:00:00Z",
        "evidence_digest": EVIDENCE_REF,
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "type": "occurrence_detected",
        "occurrence": {
            "occurrence_id": "OCC-1",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "observation_ids": ["OBS-1"],
            "problem_id": "PROB-1",
            "provisional_assessment": {
                "classification": "product_bug",
                "severity": "high",
                "authority": "llm_provisional",
                "root_cause_hypothesis": "x",
            },
            "analysis": {
                "evidence_bundle_digest": EVIDENCE_REF,
                "analyzer": "assurance.quality",
                "prompt_version": "1",
                "candidate_digest": EVIDENCE_REF,
            },
        },
    }


def write_set(*paths: str, digest: str = HEX_A) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id=HEX_B,
        candidate_tree_id=HEX_A,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=digest) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="capabilities-test",
        task_id="capabilities-task",
        graph_instance_id="capabilities-graph",
        node_id="capabilities-node",
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
