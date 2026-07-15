"""Write override evidence when --allow-test-changes bypasses the test-tree guard."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.workflow.execution.tree_hash import TreeHash


@dataclass(frozen=True)
class OverrideEvidenceResult:
    rel_path: str
    sha256: str
    changed_files_count: int


def write_test_changes_override_evidence(
    *,
    project_root: Path,
    change_id: str,
    batch_id: str,
    batch_dir: Path,
    reason: str,
    integrity: TreeHash,
    created_at: str | None = None,
) -> OverrideEvidenceResult:
    batch_dir.mkdir(parents=True, exist_ok=True)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "change_id": change_id,
        "batch_id": batch_id,
        "reason": reason,
        "created_at": timestamp,
        "tests_tree_sha256": integrity.aggregate,
        "test_files_sha256": integrity.files,
        "changed_files_count": len(integrity.files),
    }
    json_path = batch_dir / "test-changes-override.json"
    json_text = json.dumps(payload, indent=2)
    json_path.write_text(json_text, encoding="utf-8")

    diff_path = batch_dir / "test-changes-override.diff"
    try:
        result = subprocess.run(
            ["git", "diff", "--", "tests/"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        diff_path.write_text(result.stdout or "", encoding="utf-8")
    except OSError:
        diff_path.write_text("", encoding="utf-8")

    rel = f"runs/{batch_id}/test-changes-override.json"
    return OverrideEvidenceResult(
        rel_path=rel,
        sha256=hashlib.sha256(json_text.encode("utf-8")).hexdigest(),
        changed_files_count=len(integrity.files),
    )
