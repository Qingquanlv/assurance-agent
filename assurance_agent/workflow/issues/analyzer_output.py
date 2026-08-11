"""Deterministic completion of the Issue analyzer's paired outputs.

The agent authors the semantic candidate document and analysis status.  The
runtime owns the digest that binds those two files, because asking an LLM to
reproduce a canonical serialization/hash algorithm is both unreliable and an
unnecessary reason for it to create helper scripts in the task workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import IssueAnalysisStatus, IssueCandidateDocument
from assurance_agent.workflow.issues.identity import candidate_document_digest

_CANDIDATES_OUTPUT = "change:inspect/issue-candidates.json"
_STATUS_OUTPUT = "change:inspect/issue-analysis-status.json"

# Agents often emit qualitative labels; map them to schema floats before validate.
_CONFIDENCE_LABELS: dict[str, float] = {
    "low": 0.25,
    "medium": 0.5,
    "high": 0.85,
    "certain": 0.95,
}


class IssueAnalyzerOutputError(ValueError):
    """The analyzer's authored output pair cannot be completed safely."""


def _coerce_confidence(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _CONFIDENCE_LABELS:
            return _CONFIDENCE_LABELS[key]
        try:
            return float(value.strip())
        except ValueError:
            return value
    return value


def _coerce_candidate_confidences(candidate_data: dict[str, Any]) -> bool:
    """Mutate candidate confidences in place. Returns True if any value changed."""
    candidates = candidate_data.get("candidates")
    if not isinstance(candidates, list):
        return False
    changed = False
    for candidate in candidates:
        if not isinstance(candidate, dict) or "confidence" not in candidate:
            continue
        coerced = _coerce_confidence(candidate["confidence"])
        if coerced != candidate["confidence"]:
            candidate["confidence"] = coerced
            changed = True
    return changed


def complete_issue_analyzer_outputs(change_dir: Path, outputs: tuple[str, ...]) -> None:
    """Stamp the runtime-owned canonical candidate digest before write-set freeze.

    The function is deliberately a no-op unless the node declares the complete
    Issue analyzer output pair, so other skills and partial test nodes are never
    mutated implicitly.
    """
    if not {_CANDIDATES_OUTPUT, _STATUS_OUTPUT}.issubset(outputs):
        return

    candidates_path = change_dir / "inspect" / "issue-candidates.json"
    status_path = change_dir / "inspect" / "issue-analysis-status.json"
    try:
        candidate_data = json.loads(candidates_path.read_text(encoding="utf-8"))
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IssueAnalyzerOutputError(f"cannot read Issue analyzer outputs: {exc}") from exc
    if not isinstance(candidate_data, dict) or not isinstance(status_data, dict):
        raise IssueAnalyzerOutputError("Issue analyzer outputs must be JSON objects")

    if _coerce_candidate_confidences(candidate_data):
        candidates_path.write_text(
            json.dumps(candidate_data, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    try:
        IssueCandidateDocument.model_validate(candidate_data)
        status = IssueAnalysisStatus.model_validate(status_data)
    except ValidationError as exc:
        raise IssueAnalyzerOutputError(f"invalid Issue analyzer output: {exc}") from exc

    if status.status != "completed":
        return

    completed_status = dict(status_data)
    completed_status["candidate_digest"] = candidate_document_digest(candidate_data)
    status_path.write_text(
        json.dumps(completed_status, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
