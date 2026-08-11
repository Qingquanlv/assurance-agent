"""materialize-quarantine-projection from C3 receipts (M3 Task 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.quarantine import QuarantineProjection
from assurance_agent.evidence.replay_telemetry import replay_receipt_relpath
from assurance_agent.workflow.discovery.replay_receipts import write_replay_attempt_receipts
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.quarantine import (
    QUARANTINE_PROJECTION_REL,
    materialize_quarantine_projection,
    materialize_quarantine_projection_operation,
)


def _receipt(
    *,
    ce_id: str,
    outcome: str,
    attempt_index: int,
) -> ReplayAttemptReceipt:
    return ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id=ce_id,
        seed=42,
        attempt_index=attempt_index,
        base_revision="deadbeef",
        oracle_set_digest="sha256:" + ("c" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("d" * 64),
    )


def _ce_yaml(ce_id: str, obligation_ids: tuple[str, ...]) -> str:
    obs = "[]" if not obligation_ids else "\n" + "\n".join(f'  - "{oid}"' for oid in obligation_ids)
    return f"""schema_version: "1"
counterexample_id: {ce_id}
campaign_id: CAMP-1
round_id: R1
surface: api
technique: boundary
obligation_ids: {obs}
oracle_id: ORACLE-1
oracle_kind: hard_oracle
environment_digest: env1
seed: 42
minimization:
  status: minimized
  parent_counterexample_id: null
replay:
  attempts: 2
  reproduced: 1
  artifact_refs: []
finding_status: needs_review
"""


def test_materialize_enters_flaky_obligation_from_receipts(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-001"
    change_dir.mkdir(parents=True)
    ce_id = "CE-FLAKY"
    ce_path = change_dir / "discovery" / "counterexamples" / f"{ce_id}.yaml"
    ce_path.parent.mkdir(parents=True)
    key = "entities.dept.constraints.name_unique"
    ce_path.write_text(_ce_yaml(ce_id, (key,)), encoding="utf-8")
    write_replay_attempt_receipts(
        change_dir,
        (
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=0),
            _receipt(ce_id=ce_id, outcome="hold", attempt_index=1),
        ),
    )

    projection = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-001",
        entered_at="2026-08-05T12:00:00Z",
    )
    assert any(e.subject_key == key and e.status == "active" for e in projection.entries)
    active = next(e for e in projection.entries if e.subject_key == key)
    assert active.evidence_refs
    assert replay_receipt_relpath(counterexample_id=ce_id, attempt_index=0) in active.evidence_refs

    out = change_dir / QUARANTINE_PROJECTION_REL
    assert out.is_file()
    loaded = QuarantineProjection.model_validate_json(out.read_text(encoding="utf-8"))
    assert loaded.change_id == "CH-Q-001"


def test_materialize_releases_after_consecutive_successes(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-002"
    change_dir.mkdir(parents=True)
    ce_id = "CE-RECOVER"
    key = "admin_creates_dept"
    ce_path = change_dir / "discovery" / "counterexamples" / f"{ce_id}.yaml"
    ce_path.parent.mkdir(parents=True)
    ce_path.write_text(_ce_yaml(ce_id, (key,)), encoding="utf-8")

    # First pass: flaky → enter
    write_replay_attempt_receipts(
        change_dir,
        (
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=0),
            _receipt(ce_id=ce_id, outcome="hold", attempt_index=1),
        ),
    )
    first = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-002",
        entered_at="2026-08-05T12:00:00Z",
    )
    assert first.entries[0].status == "active"

    # Second pass appends two trailing violates → release (default N=2).
    # Existing attempt identities are immutable and cannot be rewritten.
    write_replay_attempt_receipts(
        change_dir,
        (
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=2),
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=3),
        ),
    )
    second = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-002",
        entered_at="2026-08-05T13:00:00Z",
    )
    entry = next(e for e in second.entries if e.subject_key == key)
    assert entry.status == "released"
    assert entry.consecutive_successes >= 2

    # Replaying the exact same aggregate is idempotent. Historical flaky
    # receipts must not make a released subject active again.
    third = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-002",
        entered_at="2026-08-05T14:00:00Z",
    )
    assert next(e for e in third.entries if e.subject_key == key).status == "released"

    # Only genuinely new failing evidence may re-enter quarantine.
    write_replay_attempt_receipts(
        change_dir,
        (_receipt(ce_id=ce_id, outcome="hold", attempt_index=4),),
    )
    fourth = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-002",
        entered_at="2026-08-05T15:00:00Z",
    )
    assert next(e for e in fourth.entries if e.subject_key == key).status == "active"


def test_materialize_fails_closed_when_receipt_counterexample_is_missing(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-MISSING-CE"
    change_dir.mkdir(parents=True)
    write_replay_attempt_receipts(
        change_dir,
        (_receipt(ce_id="CE-NOT-PRESENT", outcome="hold", attempt_index=0),),
    )

    with pytest.raises(ValueError, match="CE-NOT-PRESENT"):
        materialize_quarantine_projection(
            change_dir=change_dir,
            change_id="CH-Q-MISSING-CE",
            entered_at="2026-08-05T12:00:00Z",
        )


@pytest.mark.parametrize("conflict_kind", ("wrong_filename", "duplicate_id"))
def test_materialize_rejects_counterexample_file_identity_conflicts(
    tmp_path: Path,
    conflict_kind: str,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-CE-IDENTITY"
    change_dir.mkdir(parents=True)
    ce_root = change_dir / "discovery" / "counterexamples"
    ce_root.mkdir(parents=True)
    ce_id = "CE-A"
    if conflict_kind == "wrong_filename":
        (ce_root / "WRONG.yaml").write_text(
            _ce_yaml(ce_id, ("journey-a",)),
            encoding="utf-8",
        )
    else:
        (ce_root / "CE-A.yaml").write_text(
            _ce_yaml(ce_id, ("journey-a",)),
            encoding="utf-8",
        )
        (ce_root / "CE-A.yml").write_text(
            _ce_yaml(ce_id, ("journey-b",)),
            encoding="utf-8",
        )
    write_replay_attempt_receipts(
        change_dir,
        (_receipt(ce_id=ce_id, outcome="hold", attempt_index=0),),
    )

    with pytest.raises(ValueError, match="counterexample"):
        materialize_quarantine_projection(
            change_dir=change_dir,
            change_id="CH-Q-CE-IDENTITY",
            entered_at="2026-08-05T12:00:00Z",
        )


def test_materialize_allows_present_counterexample_with_no_obligations(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-NO-OBLIGATIONS"
    change_dir.mkdir(parents=True)
    ce_id = "CE-NO-OBLIGATIONS"
    ce_path = change_dir / "discovery" / "counterexamples" / f"{ce_id}.yaml"
    ce_path.parent.mkdir(parents=True)
    ce_path.write_text(_ce_yaml(ce_id, ()), encoding="utf-8")
    write_replay_attempt_receipts(
        change_dir,
        (_receipt(ce_id=ce_id, outcome="hold", attempt_index=0),),
    )

    projection = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id="CH-Q-NO-OBLIGATIONS",
        entered_at="2026-08-05T12:00:00Z",
    )

    assert projection.entries == ()


def test_materialize_refuses_to_replace_corrupt_prior_projection(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-Q-CORRUPT"
    projection_path = change_dir / QUARANTINE_PROJECTION_REL
    projection_path.parent.mkdir(parents=True)
    projection_path.write_text("{not-json", encoding="utf-8")
    before = projection_path.read_bytes()

    with pytest.raises(ValueError, match="quarantine projection"):
        materialize_quarantine_projection(
            change_dir=change_dir,
            change_id="CH-Q-CORRUPT",
            entered_at="2026-08-05T12:00:00Z",
        )

    assert projection_path.read_bytes() == before


def test_materialize_operation_writes_inspect_artifact(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    change_dir = project / "qa" / "changes" / "CH-Q-003"
    change_dir.mkdir(parents=True)
    (change_dir / "inspect").mkdir(parents=True)
    workspace = TaskWorkspace(
        task_id="t-q",
        root=project,
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )
    task = ExecutableTask.model_construct(
        task_id="t-q",
        node_id="materialize-quarantine-projection",
        graph_id="metrics-nightly-workflow",
        target="operation:materialize-quarantine-projection",
        input={"with": {"entered_at": "2026-08-05T14:00:00Z"}},
    )
    result = materialize_quarantine_projection_operation(
        task,
        workspace,
        RuntimeContext.model_construct(
            project_root=project,
            repo_root=project,
            change_dir=change_dir,
            change_id="CH-Q-003",
            params={},
        ),
    )
    assert result.status == "succeeded"
    path = change_dir / QUARANTINE_PROJECTION_REL
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["change_id"] == "CH-Q-003"
    assert payload["entries"] == []
