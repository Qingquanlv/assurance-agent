from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from compare import COMPARED_FIELDS, SET_FIELDS
from projection import (
    BehavioralProjectionV1,
    JSONValue,
    ProjectionError,
    ProjectionModel,
    digest_export,
    jsonable,
    project_new_export,
)


class EvalFindingV1(ProjectionModel):
    field: str
    mode: Literal["exact", "set", "predicate", "intentionally-different"]
    outcome: Literal["pass", "fail", "intentionally-different"]
    evidence_digest: str


class ExternalEvalV1(ProjectionModel):
    schema_version: Literal["1"]
    opencode_export_digest: str
    cursor_export_digest: str
    findings: tuple[EvalFindingV1, ...]
    retro_input_digest: str


def evaluate_complete_runs(opencode_export: Path, cursor_export: Path) -> ExternalEvalV1:
    opencode = _completed_projection(opencode_export)
    cursor = _completed_projection(cursor_export)
    findings = tuple(
        _finding(field, getattr(opencode, field), getattr(cursor, field)) for field in COMPARED_FIELDS
    )
    payload = jsonable(
        {
            "opencode_export_digest": digest_export(opencode_export),
            "cursor_export_digest": digest_export(cursor_export),
            "findings": tuple(item.model_dump(mode="json") for item in findings),
        }
    )
    return ExternalEvalV1(
        schema_version="1",
        opencode_export_digest=digest_export(opencode_export),
        cursor_export_digest=digest_export(cursor_export),
        findings=findings,
        retro_input_digest=_digest_json(payload),
    )


def _completed_projection(export_root: Path) -> BehavioralProjectionV1:
    projection = project_new_export(export_root)
    if projection.terminal_class != "completed":
        raise ProjectionError("evaluate_complete_runs requires completed exports")
    return projection


def _finding(field: str, left: object, right: object) -> EvalFindingV1:
    dumped_left = jsonable(left)
    dumped_right = jsonable(right)
    if field == "runtime_identity":
        mode: Literal["exact", "set", "predicate", "intentionally-different"] = "intentionally-different"
        outcome: Literal["pass", "fail", "intentionally-different"] = (
            "intentionally-different" if dumped_left != dumped_right else "fail"
        )
    elif field in SET_FIELDS:
        mode = "set"
        outcome = "pass" if dumped_left == dumped_right else "fail"
    else:
        mode = "exact"
        outcome = "pass" if dumped_left == dumped_right else "fail"
    return EvalFindingV1(
        field=field,
        mode=mode,
        outcome=outcome,
        evidence_digest=_digest_json({"field": field, "left": dumped_left, "right": dumped_right}),
    )


def _digest_json(value: JSONValue) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _json_default(value: object) -> object:
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value)!r}")
