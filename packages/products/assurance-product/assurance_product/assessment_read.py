"""Read one committed obligation assessment.

The caller names the change, plan digest, coverage epoch, and batch. This
module opens only those paths. It does not scan sibling batches or pick the
newest file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from assurance_quality.contracts.obligations import ObligationAssessmentV1

_ASSESSMENT_NAME = "obligation-assessment.json"


def read_committed_assessment(
    *,
    project_dir: Path,
    change_id: str,
    plan_digest: str,
    coverage_epoch: str,
    batch_id: str,
) -> dict[str, object]:
    identity = {
        "change_id": change_id,
        "plan_digest": plan_digest,
        "coverage_epoch": coverage_epoch,
        "batch_id": batch_id,
    }
    plan_relative = f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json"
    batch_relative = f"qa/results/inspect/epochs/{coverage_epoch}/batches/{batch_id}"
    manifest_relative = f"{batch_relative}/issue-evidence-manifest.json"
    assessment_relative = f"{batch_relative}/{_ASSESSMENT_NAME}"
    plan_path = project_dir / plan_relative
    manifest_path = project_dir / manifest_relative
    assessment_path = project_dir / assessment_relative
    if not plan_path.is_file() and not manifest_path.is_file() and not assessment_path.is_file():
        return {**identity, "assessment": None, "reason": "not_assessed"}
    plan_bytes = _read_bytes(plan_path)
    if plan_bytes is None or hashlib.sha256(plan_bytes).hexdigest() != plan_digest:
        return {**identity, "assessment": None, "reason": "plan_mismatch"}
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        return {**identity, "assessment": None, "reason": "missing_ref"}
    if manifest.get("change_id") != change_id or manifest.get("batch_id") != batch_id:
        return {**identity, "assessment": None, "reason": "missing_ref"}
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return {**identity, "assessment": None, "reason": "missing_ref"}
    digest = _entry_digest(entries, assessment_relative)
    if digest is None:
        return {**identity, "assessment": None, "reason": "missing_ref"}
    assessment_bytes = _read_bytes(assessment_path)
    if assessment_bytes is None:
        return {**identity, "assessment": None, "reason": "missing_ref"}
    actual = hashlib.sha256(assessment_bytes).hexdigest()
    if actual != digest:
        return {**identity, "assessment": None, "reason": "assessment_changed"}
    try:
        parsed = ObligationAssessmentV1.model_validate_json(assessment_bytes)
    except (ValidationError, ValueError):
        return {**identity, "assessment": None, "reason": "not_assessed"}
    if parsed.plan_ref.digest != plan_digest or parsed.plan_ref.path != plan_relative:
        return {**identity, "assessment": None, "reason": "plan_mismatch"}
    return {
        **identity,
        "assessment": parsed.model_dump(mode="json"),
        "reason": None,
    }


def _entry_digest(entries: list[object], assessment_relative: str) -> str | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("path") != assessment_relative:
            continue
        digest = entry.get("digest")
        if not isinstance(digest, str) or not digest:
            return None
        hex_digest = digest.removeprefix("sha256:")
        if len(hex_digest) != 64 or any(character not in "0123456789abcdef" for character in hex_digest):
            return None
        return hex_digest
    return None


def _read_bytes(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_json(path: Path) -> object | None:
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        return cast(object, json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        return None
