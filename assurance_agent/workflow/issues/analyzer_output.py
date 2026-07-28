"""Deterministic completion of the Issue analyzer's paired outputs.

The agent authors the semantic candidate document and analysis status.  The
runtime owns the digest that binds those two files, because asking an LLM to
reproduce a canonical serialization/hash algorithm is both unreliable and an
unnecessary reason for it to create helper scripts in the task workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import IssueAnalysisStatus, IssueCandidateDocument
from assurance_agent.workflow.issues.identity import candidate_document_digest

_CANDIDATES_OUTPUT = "change:inspect/issue-candidates.json"
_STATUS_OUTPUT = "change:inspect/issue-analysis-status.json"


class IssueAnalyzerOutputError(ValueError):
    """The analyzer's authored output pair cannot be completed safely."""


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
