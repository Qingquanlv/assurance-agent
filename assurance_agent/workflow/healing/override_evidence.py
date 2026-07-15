"""Build override evidence when --allow-test-changes bypasses the test-tree guard.

Pure content producer: no Path I/O, no datetime.now(), no subprocess.
Callers supply precomputed values and write via progression.transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from assurance_agent.workflow.execution.tree_hash import TreeHash


@dataclass(frozen=True)
class OverrideEvidenceContent:
    json_bytes: bytes
    diff_bytes: bytes
    sha256: str
    rel_path: str
    changed_files_count: int


def build_test_changes_override_evidence(
    *,
    change_id: str,
    batch_id: str,
    reason: str,
    integrity: TreeHash,
    created_at: str,
    diff_text: str,
) -> OverrideEvidenceContent:
    payload = {
        "change_id": change_id,
        "batch_id": batch_id,
        "reason": reason,
        "created_at": created_at,
        "tests_tree_sha256": integrity.aggregate,
        "test_files_sha256": integrity.files,
        "changed_files_count": len(integrity.files),
    }
    json_text = json.dumps(payload, indent=2)
    json_bytes = json_text.encode("utf-8")
    return OverrideEvidenceContent(
        json_bytes=json_bytes,
        diff_bytes=diff_text.encode("utf-8"),
        sha256=hashlib.sha256(json_bytes).hexdigest(),
        rel_path=f"runs/{batch_id}/test-changes-override.json",
        changed_files_count=len(integrity.files),
    )


# Back-compat alias for tests/callers that still import the result shape.
OverrideEvidenceResult = OverrideEvidenceContent
