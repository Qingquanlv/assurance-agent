"""Path-hit Class II cross-artifact invariants.

These run on successful commits whose write-set (or declared outputs) contains
the named left/right paths. They must not be dispatched by skill name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import IssueAnalysisStatus, IssueCandidateDocument
from assurance_agent.artifacts.models.minimum_coverage import MinimumCoverageMatrix
from assurance_agent.artifacts.models.review import CaseReviewAuthoring
from assurance_agent.artifacts.paths import (
    MINIMUM_COVERAGE_MATRIX_HISTORICAL_REL,
    MINIMUM_COVERAGE_MATRIX_REL,
)
from assurance_agent.artifacts.registry import load_registered_artifact, match_artifact, parse_wire
from assurance_agent.config import load_config
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotV1
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WriteSet, WorkspaceError
from assurance_agent.workflow.healing.safety import load_product_code_roots
from assurance_agent.workflow.issues.identity import candidate_document_digest
from assurance_agent.workflow.graph.models import TaskResult

ISSUE_CANDIDATE_DIGEST = "issue_candidate_digest"
CASE_REVIEW_MRC = "case_review_mrc"
QA_YAML_CASE_AUTOMATION = "qa_yaml_case_automation"
CASE_DESIGN_SOURCE_VERIFICATION = "case_design_source_verification"

_HIT_MARKERS = (
    "inspect/issue-candidates.json",
    "inspect/issue-analysis-status.json",
    "review/case-review.json",
    "minimum-coverage-matrix",
    ".qa.yaml",
    "proposal.md",
)


def outputs_hit_invariants(outputs: Sequence[str]) -> bool:
    joined = " ".join(outputs)
    return any(marker in joined for marker in _HIT_MARKERS)


def run_cross_artifact_invariants(
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    project_root: Path | None,
    host_change_dir: Path | None = None,
) -> list[str]:
    """Run every invariant whose left/right paths are present. Return ids that ran."""
    ran: list[str] = []
    authored = _load_authored_from_write_set(store, write_set)
    candidate = authored.get("change:inspect/issue-candidates.json")
    analysis = authored.get("change:inspect/issue-analysis-status.json")
    if candidate is not None and analysis is not None:
        ran.append(ISSUE_CANDIDATE_DIGEST)
        failure = validate_issue_candidate_digest(authored)
        _raise_if_failed(failure)

    if "change:review/case-review.json" in authored:
        matrix_raw = _load_matrix_payload(store, write_set, input_snapshot)
        if matrix_raw is not None:
            ran.append(CASE_REVIEW_MRC)
            failure = validate_case_review_minimum_coverage_payloads(
                review_raw=authored["change:review/case-review.json"],
                matrix_raw=matrix_raw,
            )
            _raise_if_failed(failure)

    if "change:.qa.yaml" in authored:
        ran.append(QA_YAML_CASE_AUTOMATION)
        failure = validate_case_design_approved_automation(authored)
        _raise_if_failed(failure)

    proposal_logical = "change:proposal.md"
    if proposal_logical in write_set.outputs_sha256:
        ran.append(CASE_DESIGN_SOURCE_VERIFICATION)
        if project_root is None:
            raise ValueError("case-design source verification requires project_root")
        _require_host_project_root(project_root, host_change_dir)
        text = _load_write_set_text(store, write_set, proposal_logical)
        failure = validate_case_design_source_verification_text(
            text,
            project_root=project_root,
        )
        _raise_if_failed(failure)

    return ran


def validate_issue_candidate_digest(authored: Mapping[str, Any]) -> TaskResult | None:
    candidate = authored.get("change:inspect/issue-candidates.json")
    analysis_status = authored.get("change:inspect/issue-analysis-status.json")
    if not isinstance(candidate, dict) or not isinstance(analysis_status, dict):
        return None
    try:
        IssueCandidateDocument.model_validate(candidate)
        status_model = IssueAnalysisStatus.model_validate(analysis_status)
    except ValidationError as exc:
        return task_failure("invalid_output", f"issue candidate digest contract is invalid: {exc}")
    expected = candidate_document_digest(candidate)
    if status_model.candidate_digest != expected:
        return task_failure(
            "invalid_output",
            "output 'change:inspect/issue-analysis-status.json' candidate_digest "
            f"must equal canonical issue-candidates digest {expected!r}",
        )
    return None


def validate_case_design_approved_automation(
    authored: Mapping[str, object],
) -> TaskResult | None:
    """Reject case-design output that silently drops an approved test layer."""
    qa = authored.get("change:.qa.yaml")
    if not isinstance(qa, Mapping):
        return None
    approval = qa.get("approval")
    approach = approval.get("approved_approach") if isinstance(approval, Mapping) else None
    if not isinstance(approach, str):
        return None
    layer_case_types = {
        "api": "API",
        "e2e": "E2E",
        "fuzz": "Fuzz",
        "performance": "Performance",
    }
    selected = {
        case_type
        for token, case_type in layer_case_types.items()
        if re.search(rf"(?i)(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", approach)
    }
    if not selected:
        return None

    automated: set[str] = set()
    for logical, document in authored.items():
        if not (
            logical.startswith("change:cases/")
            and logical.endswith(("/case.yaml", "/case.yml", "/case.json"))
            and isinstance(document, Mapping)
        ):
            continue
        for bucket in ("added", "modified"):
            entries = document.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                automation = entry.get("automation")
                if isinstance(automation, Mapping) and automation.get("required") is True:
                    case_type = entry.get("type")
                    if isinstance(case_type, str):
                        automated.add(case_type)
    missing = sorted(selected - automated)
    if missing:
        return task_failure(
            "invalid_output",
            ".qa.yaml approval selected automated layers without an "
            f"automation.required=true case: {', '.join(missing)}",
        )
    return None


_CASE_DESIGN_SOURCE_VERIFICATION_REPAIR = (
    "\nRequired format:\n"
    "- independently_read: true\n"
    "- reviewed_source_files:\n"
    "  - `<project-relative product source path>`"
)


def validate_case_design_source_verification(workspace: TaskWorkspace) -> TaskResult | None:
    proposal = workspace.change_dir / "proposal.md"
    try:
        text = proposal.read_text(encoding="utf-8")
    except OSError as exc:
        return _case_design_source_failure(
            f"case-design Product Source Verification could not read proposal.md: {exc}",
        )
    return validate_case_design_source_verification_text(text, project_root=workspace.project_root)


def validate_case_design_source_verification_text(
    text: str,
    *,
    project_root: Path,
) -> TaskResult | None:
    headings = list(re.finditer(r"(?m)^##[ \t]+Product Source Verification[ \t]*$", text))
    if len(headings) != 1:
        return _case_design_source_failure(
            "case-design proposal.md must contain exactly one '## Product Source Verification' section",
        )
    start = headings[0].end()
    next_heading = re.search(r"(?m)^##[ \t]+", text[start:])
    end = start + next_heading.start() if next_heading is not None else len(text)
    section = text[start:end]
    if (
        re.search(
            r"(?mi)^[ \t]*-[ \t]*independently_read:[ \t]*true[ \t]*$",
            section,
        )
        is None
    ):
        return _case_design_source_failure(
            "case-design Product Source Verification must declare independently_read: true",
        )

    lines = section.splitlines()
    source_paths: list[str] = []
    for index, line in enumerate(lines):
        if re.fullmatch(r"[ \t]*-[ \t]*reviewed_source_files:[ \t]*", line) is None:
            continue
        for item in lines[index + 1 :]:
            match = re.fullmatch(r"[ \t]{2,}-[ \t]+(.+?)[ \t]*", item)
            if match is not None:
                source_paths.append(match.group(1).strip().strip("`\"'"))
                continue
            if item.strip():
                break
        break

    resolved_root = project_root.resolve()
    product_roots: list[Path] = []
    raw_product_roots = list(load_product_code_roots(project_root))
    try:
        config = load_config(project_root)
    except AaError:
        pass
    else:
        raw_product_roots.extend((config.sources.frontend, config.sources.backend))
    for raw_root in dict.fromkeys(raw_product_roots):
        try:
            candidate = (project_root / raw_root).resolve(strict=True)
            candidate.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if candidate.is_dir():
            product_roots.append(candidate)

    def valid_source(raw: str) -> bool:
        rel = Path(raw)
        if not raw or rel.is_absolute() or ".." in rel.parts:
            return False
        try:
            resolved = (project_root / rel).resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            return False
        return resolved.is_file() and any(_path_is_within(resolved, root) for root in product_roots)

    if not source_paths or any(not valid_source(path) for path in source_paths):
        return _case_design_source_failure(
            "case-design Product Source Verification reviewed_source_files must contain only "
            "existing project-relative files under configured product-code roots",
        )
    return None


def validate_case_review_minimum_coverage_payloads(
    *,
    review_raw: object,
    matrix_raw: object,
) -> TaskResult | None:
    try:
        review = CaseReviewAuthoring.model_validate(review_raw)
        matrix = MinimumCoverageMatrix.model_validate(matrix_raw)
    except ValidationError as exc:
        return task_failure("invalid_output", f"case review minimum_coverage contract is invalid: {exc}")

    required = [row for row in matrix.root if row.required]
    expected_total = len(required)
    expected_covered = sum(row.status == "covered" for row in required)
    expected_missing = [row.key for row in required if row.status == "skipped_by_scope"]
    expected_skipped = len(expected_missing)
    actual = review.minimum_coverage
    if (
        actual.total_required != expected_total
        or actual.covered != expected_covered
        or actual.skipped_by_scope != expected_skipped
        or actual.missing != expected_missing
    ):
        return task_failure(
            "invalid_output",
            "case review minimum_coverage disagrees with frozen matrix: "
            f"expected total_required={expected_total}, covered={expected_covered}, "
            f"skipped_by_scope={expected_skipped}, missing={expected_missing!r}; "
            f"got total_required={actual.total_required}, covered={actual.covered}, "
            f"skipped_by_scope={actual.skipped_by_scope}, missing={actual.missing!r}",
        )
    return None


def _case_design_source_failure(message: str) -> TaskResult:
    return task_failure(
        "invalid_output",
        message + _CASE_DESIGN_SOURCE_VERIFICATION_REPAIR,
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _raise_if_failed(failure: TaskResult | None) -> None:
    if failure is None:
        return
    raise ValueError(failure.error or "cross-artifact invariant failed")


def _require_host_project_root(project_root: Path, host_change_dir: Path | None) -> None:
    if host_change_dir is None:
        raise ValueError("case-design source verification requires host change_dir")
    try:
        host_change_dir.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(
            "case-design source verification cannot live-read a subgraph task workspace"
        ) from exc


def _is_case_document(rel: str) -> bool:
    return rel.startswith("cases/") and rel.endswith(("/case.yaml", "/case.yml", "/case.json"))


def _load_authored_from_write_set(store: TreeStore, write_set: WriteSet) -> dict[str, Any]:
    authored: dict[str, Any] = {}
    for logical in write_set.outputs_sha256:
        root, _, rest = logical.partition(":")
        if root != "change" or not rest:
            continue
        spec = match_artifact(rest)
        if spec is not None and spec.compat != "must_compat":
            continue
        if spec is None and not _is_case_document(rest):
            continue
        try:
            text = _load_write_set_text(store, write_set, logical)
            if spec is not None:
                authored[logical] = load_registered_artifact(rest, text)
            else:
                authored[logical] = parse_wire("json" if rest.endswith(".json") else "yaml", text)
        except (ValueError, yaml.YAMLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable write-set artifact {logical}: {exc}") from exc
    return authored


def _load_write_set_text(store: TreeStore, write_set: WriteSet, logical: str) -> str:
    digest = write_set.outputs_sha256.get(logical)
    if digest is None:
        raise ValueError(f"missing write-set artifact: {logical}")
    try:
        return store.read_object(digest.removeprefix("sha256:")).decode("utf-8")
    except (WorkspaceError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable write-set artifact {logical}: {exc}") from exc


def _load_matrix_payload(
    store: TreeStore,
    write_set: WriteSet,
    snapshot: TaskInputSnapshotV1,
) -> object | None:
    """Resolve the matrix from the write-set, then the pinned snapshot. Never live disk.

    Returns None when neither source has the matrix so the invariant stays
    skipped rather than reading the producer workspace.
    """
    logicals = (
        f"change:{MINIMUM_COVERAGE_MATRIX_REL}",
        f"change:{MINIMUM_COVERAGE_MATRIX_HISTORICAL_REL}",
    )
    text = None
    used = logicals[0]
    for logical in logicals:
        if logical in write_set.outputs_sha256:
            text = _load_write_set_text(store, write_set, logical)
            used = logical
            break
    if text is None:
        for logical in logicals:
            for entry in snapshot.entries:
                if entry.kind != "file" or entry.sha256 is None:
                    continue
                if logical not in set(entry.logical_aliases):
                    continue
                try:
                    text = store.read_object(entry.sha256.removeprefix("sha256:")).decode("utf-8")
                except (WorkspaceError, UnicodeDecodeError) as exc:
                    raise ValueError(f"unreadable snapshot matrix: {exc}") from exc
                used = logical
                break
            if text is not None:
                break
    if text is None:
        return None
    try:
        return load_registered_artifact(used.removeprefix("change:"), text)
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid minimum-coverage-matrix: {exc}") from exc
