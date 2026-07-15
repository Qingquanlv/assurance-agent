"""Healing safety boundary: tree-hash guards, override evidence, record-apply."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from assurance_agent.config import load_config
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import (
    EventWriteError,
    HealRecordApplyEvent,
    append_event_strict,
    read_events,
)
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.execution.tree_hash import (
    diff_trees,
    hash_product_tree,
    hash_test_tree,
    sha256_file,
)
from assurance_agent.workflow.orchestration.healing_state import (
    HealingStateSnapshot,
    derive_healing_state,
)

_DEFAULT_PRODUCT_ROOTS = ["app", "web/src", "src"]


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
    change_dir = project_root / "qa" / "changes" / change_id
    snapshot = derive_healing_state(change_dir)
    allocations = [
        event for event in read_events(change_dir)
        if event.get("type") == "healing_attempt_allocated"
        and event.get("episode_id") == snapshot.episode_id
    ]
    def _event_seq(event: dict[str, object]) -> int:
        seq = event.get("seq")
        return seq if isinstance(seq, int) else 0

    latest = max(allocations, key=_event_seq, default=None)
    proposal_path = change_dir / "healing" / "fix-proposal.json"
    proposal_sha = sha256_file(proposal_path)
    source_batch = str(latest["source_batch_id"]) if latest is not None else None
    return HealingGuardContext(
        snapshot=snapshot,
        source_batch_id=source_batch,
        proposal_sha256=proposal_sha,
        attempt_key=(f"{proposal_sha}:{source_batch}" if proposal_sha and source_batch else None),
    )


def is_healing_run_context(project_root: Path, change_id: str) -> bool:
    snapshot = derive_healing_state(project_root / "qa" / "changes" / change_id)
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
    change_dir = project_root / "qa" / "changes" / change_id
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

    change_dir = project_root / "qa" / "changes" / change_id
    _, _, baseline_sha = _load_manifest_hashes(change_dir)
    if baseline_sha is None:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])

    product_roots = roots if roots is not None else load_product_code_roots(project_root)
    current = hash_product_tree(project_root, product_roots)
    if current.aggregate == baseline_sha:
        return ProductTreeIntegrity(product_changed=False, changed_files=[])

    raise HealingGuardError(
        "PRODUCT-CHANGED-DURING-HEALING: product code tree changed during healing"
    )


def pin_healing_applied_test_tree(project_root: Path, change_id: str) -> Path:
    change_dir = project_root / "qa" / "changes" / change_id
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


def record_apply_summary(
    project_root: Path,
    change_id: str,
    target: str,
    proposal_ids: list[str],
) -> RecordApplySummaryResult:
    if target not in ("api", "e2e"):
        raise HealingGuardError(f"unsupported heal target: {target}")

    change_dir = project_root / "qa" / "changes" / change_id
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

    authorized = _authorized_files(proposal_raw, proposal_ids, target)
    if not authorized:
        raise HealingGuardError("no authorized proposals matched the given proposal ids")

    _, baseline_files, _ = _load_manifest_hashes(change_dir)
    current = hash_test_tree(project_root)
    modified = [f for f in diff_trees(baseline_files, current.files) if f in authorized]
    unauthorized = [f for f in diff_trees(baseline_files, current.files) if f not in authorized]
    if unauthorized:
        raise HealingGuardError(
            f"modified files outside authorized proposals: {', '.join(unauthorized)}"
        )

    healing_dir = change_dir / "healing"
    healing_dir.mkdir(parents=True, exist_ok=True)
    json_path = healing_dir / f"{target}-apply-summary.json"
    md_path = healing_dir / f"{target}-apply-summary.md"
    summary = {
        "schema_version": "1.0",
        "target": target,
        "applied": bool(modified),
        "proposal_ids": proposal_ids,
        "files_modified": modified,
        "source_batch_id": context.source_batch_id,
        "attempt_key": context.attempt_key,
    }
    summary_text = json.dumps(summary, indent=2)
    md_text = "\n".join([
        f"# Apply Summary — {target}",
        "",
        f"- Applied: {summary['applied']}",
        f"- Files modified: {len(modified)}",
        *(f"- {f}" for f in modified),
        "",
    ])

    snapshots = capture_files((json_path, md_path, change_dir / "events.jsonl"))
    try:
        json_path.write_text(summary_text, encoding="utf-8")
        md_path.write_text(md_text, encoding="utf-8")
        summary_sha = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
        append_event_strict(change_dir, HealRecordApplyEvent(
            target=target,  # type: ignore[arg-type]
            proposal_sha256=context.proposal_sha256,
            source_batch_id=context.source_batch_id,
            attempt_key=context.attempt_key,
            summary_sha256=summary_sha,
            files_modified=modified,
        ))
    except EventWriteError:
        restore_files(snapshots)
        raise

    return RecordApplySummaryResult(
        json_path=str(json_path),
        md_path=str(md_path),
        summary_sha256=summary_sha,
        files_modified=modified,
    )
