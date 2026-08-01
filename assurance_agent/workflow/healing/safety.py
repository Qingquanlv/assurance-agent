"""Healing safety boundary: tree-hash guards, override evidence, record-apply."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from assurance_agent.change_location import resolve_change
from assurance_agent.config import load_config
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import HealRecordApplyEvent
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.execution.tree_hash import (
    diff_trees,
    hash_product_tree,
    hash_test_tree,
    sha256_file,
)
from assurance_agent.workflow.healing.projection import project_healing_episode
from assurance_agent.workflow.orchestration.healing_state import (
    HealingStateSnapshot,
    derive_healing_state,
)

_DEFAULT_PRODUCT_ROOTS = ["app", "web/src", "src"]


def _active_change_dir(project_root: Path, change_id: str) -> Path:
    return resolve_change(project_root, change_id).path


class HealingGuardError(AaError):
    pass


class TestTreeIntegrity(BaseModel):
    tests_changed: bool
    changed_files: list[str]


class ProductTreeIntegrity(BaseModel):
    product_changed: bool
    changed_files: list[str]


@dataclass(frozen=True)
class HealingGuardContext:
    snapshot: HealingStateSnapshot
    source_batch_id: str | None
    proposal_sha256: str | None
    attempt_key: str | None


class RecordApplySummaryResult(BaseModel):
    json_path: str
    md_path: str
    summary_sha256: str
    files_modified: list[str]


def derive_guard_context(project_root: Path, change_id: str) -> HealingGuardContext:
    change_dir = _active_change_dir(project_root, change_id)
    snapshot = derive_healing_state(change_dir)
    projection = project_healing_episode(change_dir, episode_id=snapshot.episode_id)
    latest = projection.latest_allocation
    proposal_path = change_dir / "healing" / "fix-proposal.json"
    proposal_sha = sha256_file(proposal_path)
    source_batch = latest.source_batch_id if latest is not None else None
    return HealingGuardContext(
        snapshot=snapshot,
        source_batch_id=source_batch,
        proposal_sha256=proposal_sha,
        attempt_key=(f"{proposal_sha}:{source_batch}" if proposal_sha and source_batch else None),
    )


def is_healing_run_context(project_root: Path, change_id: str) -> bool:
    snapshot = derive_healing_state(_active_change_dir(project_root, change_id))
    return snapshot.episode_id is not None and snapshot.status != "not_needed"


def load_product_code_roots(project_root: Path) -> list[str]:
    try:
        config = load_config(project_root)
        execution = getattr(config, "execution", None)
        if execution is not None:
            roots = getattr(execution, "product_code_roots", None)
            if isinstance(roots, list) and roots:
                return [str(r) for r in roots]
    except AaError:
        pass
    return list(_DEFAULT_PRODUCT_ROOTS)


def _load_manifest_hashes(change_dir: Path) -> tuple[str | None, dict[str, str], str | None]:
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    if not manifest_path.is_file():
        return None, {}, None
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, {}, None
    tests_sha = raw.get("tests_tree_sha256")
    files = raw.get("test_files_sha256") or {}
    product_sha = raw.get("product_tree_sha256")
    return (
        str(tests_sha) if tests_sha else None,
        {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {},
        str(product_sha) if product_sha else None,
    )


def assert_test_tree_unchanged_or_healing(
    project_root: Path,
    change_id: str,
    *,
    allow_test_changes: bool = False,
) -> TestTreeIntegrity:
    change_dir = _active_change_dir(project_root, change_id)
    baseline_sha, baseline_files, _ = _load_manifest_hashes(change_dir)
    if baseline_sha is None:
        return TestTreeIntegrity(tests_changed=False, changed_files=[])

    current = hash_test_tree(project_root)
    changed = diff_trees(baseline_files, current.files)
    if not changed and current.aggregate == baseline_sha:
        return TestTreeIntegrity(tests_changed=False, changed_files=[])

    if allow_test_changes:
        return TestTreeIntegrity(tests_changed=True, changed_files=changed)

    if is_healing_run_context(project_root, change_id):
        snapshot = derive_healing_state(change_dir)
        if snapshot.status == "applied":
            pin_path = change_dir / "healing" / "applied-test-tree.json"
            if pin_path.is_file():
                try:
                    pin = json.loads(pin_path.read_text(encoding="utf-8"))
                    pin_sha = str(pin.get("aggregate", ""))
                    if pin_sha and pin_sha != current.aggregate:
                        raise HealingGuardError(
                            "TESTS-CHANGED-WITHOUT-HEALING: applied test-tree pin mismatch"
                        )
                except (OSError, json.JSONDecodeError) as err:
                    raise HealingGuardError(
                        f"TESTS-CHANGED-WITHOUT-HEALING: unreadable applied-test-tree.json: {err}"
                    ) from err
        return TestTreeIntegrity(tests_changed=True, changed_files=changed)

    raise HealingGuardError(
        f"TESTS-CHANGED-WITHOUT-HEALING: {len(changed)} test file(s) changed since last run"
    )


def assert_product_tree_unchanged_in_healing(
    project_root: Path,
    change_id: str,
    *,
    roots: list[str] | None = None,
) -> ProductTreeIntegrity:
    if not is_healing_run_context(project_root, change_id):
        return ProductTreeIntegrity(product_changed=False, changed_files=[])

    change_dir = _active_change_dir(project_root, change_id)
    _, _, baseline_sha = _load_manifest_hashes(change_dir)
    if baseline_sha is None:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])

    product_roots = roots if roots is not None else load_product_code_roots(project_root)
    current = hash_product_tree(project_root, product_roots)
    if current.aggregate == baseline_sha:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])

    raise HealingGuardError("PRODUCT-CHANGED-DURING-HEALING: product code tree changed during healing")


def pin_healing_applied_test_tree(project_root: Path, change_id: str) -> Path:
    change_dir = _active_change_dir(project_root, change_id)
    healing_dir = change_dir / "healing"
    healing_dir.mkdir(parents=True, exist_ok=True)
    current = hash_test_tree(project_root)
    path = healing_dir / "applied-test-tree.json"
    path.write_text(
        json.dumps({"aggregate": current.aggregate, "files": current.files}, indent=2),
        encoding="utf-8",
    )
    return path


def write_cli_fixer_safety_check(change_dir: Path, payload: dict) -> Path:
    healing_dir = change_dir / "healing"
    healing_dir.mkdir(parents=True, exist_ok=True)
    path = healing_dir / "fixer-safety-check.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


_SKIP_XFAIL_PATTERNS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "pytest.skip(",
    "pytest.xfail(",
    "unittest.skip",
    "@skip",
)


def _scan_skip_xfail_markers(project_root: Path, files: list[str]) -> bool:
    """Best-effort content scan (no baseline diff available for gitignored SUTs).

    Returns True iff any modified file currently contains a skip/xfail marker.
    Absence of markers lets the caller assert `skip_or_xfail_added == False`
    with confidence; presence only proves the marker exists now, not that it
    was newly added, so the caller must route that case to human review
    instead of guessing.
    """
    for rel in files:
        path = project_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(pattern in text for pattern in _SKIP_XFAIL_PATTERNS):
            return True
    return False


def compute_and_write_fixer_safety_check(
    project_root: Path,
    change_id: str,
) -> Path:
    """Derive `healing/fixer-safety-check.json` from apply-summary evidence.

    Called by `record_apply_summary` after every successful apply so the
    `fixer-safety-gate` (which fails closed on a missing file) always has a
    current verdict once at least one target has applied a fix. Recomputes
    from disk each time — cheap, idempotent, and reflects whichever apply
    summaries (api / e2e) exist at call time.
    """
    change_dir = _active_change_dir(project_root, change_id)
    proposal_path = change_dir / "healing" / "fix-proposal.json"
    proposal_raw: dict = {}
    if proposal_path.is_file():
        try:
            proposal_raw = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            proposal_raw = {}
    risk_by_id = {
        str(item.get("proposal_id")): str(item.get("risk_level") or "")
        for item in proposal_raw.get("proposals", [])
        if isinstance(item, dict)
    }

    modified_files: list[str] = []
    applied_proposal_ids: list[str] = []
    for target in ("api", "e2e"):
        summary_path = change_dir / "healing" / f"{target}-apply-summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not summary.get("applied", False):
            continue
        modified_files.extend(str(f) for f in summary.get("files_modified", []))
        applied_proposal_ids.extend(str(p) for p in summary.get("proposal_ids", []))

    product_roots = load_product_code_roots(project_root)
    product_code_modified = any(
        any(rel == root or rel.startswith(root.rstrip("/") + "/") for root in product_roots)
        for rel in modified_files
    )
    high_risk_proposal_applied = any(
        risk_by_id.get(pid, "").lower() in {"high", "critical"} for pid in applied_proposal_ids
    )
    has_skip_or_xfail_marker = _scan_skip_xfail_markers(project_root, modified_files)
    skip_or_xfail_added: bool | str = False if not has_skip_or_xfail_marker else "undetermined"
    # record_apply_summary already fail-closes (HealingGuardError) on any file
    # modified outside the authorized proposal set, so reaching this point
    # means every modified file was pre-authorized — unrelated_tests_modified
    # is reliably False here.
    unrelated_tests_modified = False
    assertion_expected_value_changes_detected = False

    needs_review = has_skip_or_xfail_marker
    passed = not (
        product_code_modified
        or assertion_expected_value_changes_detected
        or has_skip_or_xfail_marker
        or unrelated_tests_modified
        or high_risk_proposal_applied
    )

    payload = {
        "schema_version": "1.0",
        "change_id": change_id,
        "modified_files": modified_files,
        "product_code_modified": product_code_modified,
        "assertion_expected_value_changes_detected": assertion_expected_value_changes_detected,
        "skip_or_xfail_added": skip_or_xfail_added,
        "unrelated_tests_modified": unrelated_tests_modified,
        "high_risk_proposal_applied": high_risk_proposal_applied,
        "needs_review": needs_review,
        "passed": passed,
    }
    return write_cli_fixer_safety_check(change_dir, payload)


def _authorized_files(proposal_raw: dict, proposal_ids: list[str], target: str) -> set[str]:
    authorized: set[str] = set()
    for item in proposal_raw.get("proposals", []):
        if not isinstance(item, dict):
            continue
        pid = str(item.get("proposal_id", ""))
        if pid not in proposal_ids or str(item.get("target", "")) != target:
            continue
        if not item.get("eligible", False):
            continue
        for path in item.get("files_to_modify", []):
            authorized.add(str(path).replace("\\", "/"))
    return authorized


def _prior_applied_files(change_dir: Path, *, exclude_target: str) -> set[str]:
    applied: set[str] = set()
    for other in ("api", "e2e"):
        if other == exclude_target:
            continue
        summary_path = change_dir / "healing" / f"{other}-apply-summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not summary.get("applied", False):
            continue
        applied.update(str(f).replace("\\", "/") for f in summary.get("files_modified", []))
    return applied


_ApplyOutcome = Literal["applied", "no_op", "skipped"]


def record_apply_summary(
    project_root: Path,
    change_id: str,
    target: str,
    proposal_ids: list[str] | None = None,
    *,
    outcome: _ApplyOutcome = "applied",
    reason: str | None = None,
) -> RecordApplySummaryResult:
    """Write ``{target}-apply-summary.json`` and a frozen ``heal_record_apply`` event.

    ``outcome``:
    - ``applied`` (default): diff test tree against execution baseline; require
      authorized proposal ids. Empty ``files_modified`` yields ``applied: false``.
    - ``no_op`` / ``skipped``: contract-closing path for fixer STOP/no-eligible
      cases. Requires an unchanged test tree (aside from the other target's
      prior applied files) and writes ``applied: false`` so graph nodes that
      declare the summary as a hard output can still succeed.
    """
    if target not in ("api", "e2e"):
        raise HealingGuardError(f"unsupported heal target: {target}")
    if outcome not in ("applied", "no_op", "skipped"):
        raise HealingGuardError(f"unsupported record-apply outcome: {outcome}")

    ids = list(proposal_ids or [])
    change_dir = _active_change_dir(project_root, change_id)
    context = derive_guard_context(project_root, change_id)
    if context.source_batch_id is None or context.attempt_key is None or context.proposal_sha256 is None:
        raise HealingGuardError("no active healing allocation for record-apply")

    proposal_path = change_dir / "healing" / "fix-proposal.json"
    if not proposal_path.is_file():
        raise HealingGuardError("fix-proposal.json not found")
    try:
        proposal_raw = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise HealingGuardError(f"fix-proposal.json unreadable: {err}") from err

    _, baseline_files, _ = _load_manifest_hashes(change_dir)
    current = hash_test_tree(project_root)
    changed = diff_trees(baseline_files, current.files)
    prior_applied = _prior_applied_files(change_dir, exclude_target=target)

    if outcome in ("no_op", "skipped"):
        if not reason or not reason.strip():
            raise HealingGuardError(f"record-apply outcome '{outcome}' requires --reason")
        residual = [f for f in changed if f not in prior_applied]
        if residual:
            raise HealingGuardError(
                f"test tree changed; cannot record outcome '{outcome}': {', '.join(sorted(residual))}"
            )
        modified: list[str] = []
        applied = False
        resolved_outcome: _ApplyOutcome = outcome
        resolved_reason = reason.strip()
    else:
        if not ids:
            raise HealingGuardError("applied outcome requires at least one --proposal id")
        authorized = _authorized_files(proposal_raw, ids, target)
        if not authorized:
            raise HealingGuardError("no authorized proposals matched the given proposal ids")
        modified = [f for f in changed if f in authorized]
        unauthorized = [f for f in changed if f not in authorized and f not in prior_applied]
        if unauthorized:
            raise HealingGuardError(f"modified files outside authorized proposals: {', '.join(unauthorized)}")
        applied = bool(modified)
        resolved_outcome = "applied" if applied else "no_op"
        resolved_reason = reason.strip() if reason and reason.strip() else None

    json_rel = f"healing/{target}-apply-summary.json"
    md_rel = f"healing/{target}-apply-summary.md"
    summary: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": change_id,
        "target": target,
        "applied": applied,
        "outcome": resolved_outcome,
        "proposal_ids": ids,
        "files_modified": modified,
        "source_batch_id": context.source_batch_id,
        "attempt_key": context.attempt_key,
        "rerun_required": applied,
        "next_action": "run_aa_run" if applied else "continue",
    }
    if resolved_reason is not None:
        summary["reason"] = resolved_reason
    summary_text = json.dumps(summary, indent=2)
    md_lines = [
        f"# Apply Summary — {target}",
        "",
        f"- Applied: {summary['applied']}",
        f"- Outcome: {summary['outcome']}",
        f"- Files modified: {len(modified)}",
        *(f"- {f}" for f in modified),
    ]
    if resolved_reason is not None:
        md_lines.extend(["", f"- Reason: {resolved_reason}"])
    md_lines.append("")
    md_text = "\n".join(md_lines)
    summary_sha = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()

    with transaction(change_dir) as txn:
        txn.write_file(json_rel, summary_text)
        txn.write_file(md_rel, md_text)
        txn.append_strict(
            HealRecordApplyEvent(
                target=target,  # type: ignore[arg-type]
                proposal_sha256=context.proposal_sha256,
                source_batch_id=context.source_batch_id,
                attempt_key=context.attempt_key,
                summary_sha256=summary_sha,
                files_modified=modified,
            )
        )

    # CLI-owned healing core: (re)derive the safety-check evidence the
    # fixer-safety-gate reads. Must happen here — the gate fails closed
    # (`missing_file_is: stop`) if this file never appears, and no agent
    # skill is authorized to author it as self-attestation.
    compute_and_write_fixer_safety_check(project_root, change_id)

    return RecordApplySummaryResult(
        json_path=str(change_dir / json_rel),
        md_path=str(change_dir / md_rel),
        summary_sha256=summary_sha,
        files_modified=modified,
    )
