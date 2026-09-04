from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from projection import (
    BehavioralProjectionV1,
    JSONValue,
    ProjectionError,
    ProjectionModel,
    digest_export,
    jsonable,
    project_new_export,
)

COMPARED_FIELDS = (
    "input_digest",
    "runtime_identity",
    "terminal_class",
    "terminal_reason_category",
    "selected_families",
    "activated_families",
    "completed_families",
    "skipped_families",
    "gate_decisions",
    "artifact_contract",
    "changed_files",
    "execution_evidence",
    "quality_metrics",
    "issue_healing_decisions",
    "durable_effects",
    "report",
    "retro",
    "improvement",
    "archive",
    "semantic_counts",
    "diagnostics",
)
SET_FIELDS = frozenset(
    {
        "selected_families",
        "activated_families",
        "completed_families",
        "skipped_families",
    }
)
OPTIONAL_ABSENT_FIELDS = frozenset(
    {
        "terminal_reason_category",
        "retro",
        "improvement",
        "archive",
    }
)


class EvalFindingV1(ProjectionModel):
    field: str
    mode: Literal["exact", "set", "predicate"]
    outcome: Literal["pass", "fail"]
    evidence_digest: str


class ExternalEvalV1(ProjectionModel):
    schema_version: Literal["1"]
    export_digest: str
    findings: tuple[EvalFindingV1, ...]
    retro_input_digest: str


def evaluate_complete_run(export_root: Path) -> ExternalEvalV1:
    projection = _completed_projection(export_root)
    findings = tuple(_finding(field, getattr(projection, field)) for field in COMPARED_FIELDS)
    payload = jsonable(
        {
            "export_digest": digest_export(export_root),
            "findings": tuple(item.model_dump(mode="json") for item in findings),
        }
    )
    return ExternalEvalV1(
        schema_version="1",
        export_digest=digest_export(export_root),
        findings=findings,
        retro_input_digest=_digest_json(payload),
    )


def _completed_projection(export_root: Path) -> BehavioralProjectionV1:
    projection = project_new_export(export_root)
    if projection.terminal_class != "completed":
        raise ProjectionError("evaluate_complete_run requires a completed export")
    return projection


def _finding(field: str, value: object) -> EvalFindingV1:
    dumped = jsonable(value)
    mode: Literal["exact", "set", "predicate"] = "set" if field in SET_FIELDS else "exact"
    if field == "runtime_identity":
        outcome: Literal["pass", "fail"] = "pass" if dumped == "assurance-opencode" else "fail"
        mode = "predicate"
    elif field in OPTIONAL_ABSENT_FIELDS:
        outcome = "pass"
    else:
        outcome = "pass" if dumped is not None else "fail"
    return EvalFindingV1(
        field=field,
        mode=mode,
        outcome=outcome,
        evidence_digest=_digest_json({"field": field, "value": dumped}),
    )


def _digest_json(value: JSONValue) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _json_default(value: object) -> object:
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value)!r}")
