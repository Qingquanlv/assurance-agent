"""M3 Task 1: collect-adversarial-yield reads discovery CE receipts only."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.pr_metric_evidence import AdversarialYieldEvidence
from assurance_agent.workflow.discovery.replay_receipts import write_replay_attempt_receipts
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.adversarial_yield import (
    ADVERSARIAL_YIELD_BATCH_ID,
    ADVERSARIAL_YIELD_EVIDENCE_REL,
    collect_adversarial_yield,
    collect_adversarial_yield_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-YIELD-001"
SEED = 4242


def _workspace(project_root: Path) -> TaskWorkspace:
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    return TaskWorkspace(
        task_id="t-yield",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-yield",
        node_id="collect-adversarial-yield",
        graph_id="metrics-nightly-workflow",
        target="operation:collect-adversarial-yield",
        input={"with": {}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _confirmed_ce(
    *,
    ce_id: str = "CE-CONF-1",
    seed: int | None = SEED,
    problem_id: str | None = None,
    campaign_id: str = "CAM-1",
) -> dict:
    payload: dict = {
        "schema_version": "1",
        "counterexample_id": ce_id,
        "campaign_id": campaign_id,
        "round_id": "R1",
        "surface": "api",
        "technique": "stateful_fuzz",
        "obligation_ids": ["OBL-1"],
        "oracle_id": "ORACLE-status",
        "oracle_kind": "hard_oracle",
        "environment_digest": "envdigest",
        "generated_file_digests": {},
        "setup": {},
        "actions": [{"method": "POST", "path": "/api/x"}],
        "observed": {"status": 500},
        "expected": {"denied": True},
        "minimization": {"status": "raw", "parent_counterexample_id": None},
        "replay": {"attempts": 1, "reproduced": 1, "artifact_refs": []},
        "finding_status": "confirmed",
    }
    if seed is not None:
        payload["seed"] = seed
    if problem_id is not None:
        payload["setup"] = {"problem_id": problem_id}
    return payload


def _needs_review_ce(*, ce_id: str = "CE-NR-1", seed: int = SEED) -> dict:
    return {
        "schema_version": "1",
        "counterexample_id": ce_id,
        "campaign_id": "CAM-1",
        "round_id": "R1",
        "surface": "api",
        "technique": "stateful_fuzz",
        "obligation_ids": [],
        "oracle_id": "ORACLE-heur",
        "oracle_kind": "search_heuristic",
        "environment_digest": "envdigest",
        "generated_file_digests": {},
        "setup": {},
        "actions": [],
        "observed": {},
        "expected": {},
        "seed": seed,
        "minimization": {"status": "raw", "parent_counterexample_id": None},
        "replay": {"attempts": 0, "reproduced": 0, "artifact_refs": []},
        "finding_status": "needs_review",
    }


def _write_ce(change_dir: Path, payload: dict) -> Path:
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True, exist_ok=True)
    path = ce_dir / f"{payload['counterexample_id']}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_campaign(
    change_dir: Path,
    *,
    campaign_id: str = "CAM-1",
    counterexample_count: int = 1,
    confirmed_count: int = 1,
    sample_count: int | None = None,
    seed: int = SEED,
) -> None:
    path = change_dir / "discovery" / "campaign-result.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "campaign_id": campaign_id,
                "change_id": CHANGE_ID,
                "status": "completed",
                "surfaces": ["api"],
                "rounds_completed": 1,
                "counterexample_count": counterexample_count,
                "confirmed_count": confirmed_count,
                "sample_count": counterexample_count if sample_count is None else sample_count,
                "seed": seed,
                "stop_reason": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_closing_issue_snapshot(
    change_dir: Path,
    *,
    snapshot_change_id: str,
    counterexample_id: str,
) -> None:
    path = change_dir / "issues" / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": snapshot_change_id,
                "authoritative_batch_id": "batch-1",
                "observations": [
                    {
                        "observation_id": "OBS-1",
                        "change_id": snapshot_change_id,
                        "batch_id": "batch-1",
                        "kind": "test_failure",
                        "target": "api",
                        "source": {
                            "artifact": f"discovery/counterexamples/{counterexample_id}.yaml",
                            "json_pointer": "/finding_status",
                        },
                        "evidence_refs": [f"discovery/counterexamples/{counterexample_id}.yaml"],
                        "signature": "sig-1",
                        "observed_at": "2026-08-05T00:00:00Z",
                    }
                ],
                "occurrences": [
                    {
                        "occurrence_id": "OCC-1",
                        "change_id": snapshot_change_id,
                        "batch_id": "batch-1",
                        "observation_ids": ["OBS-1"],
                        "problem_id": "PROB-1",
                        "provisional_assessment": {
                            "classification": "product_bug",
                            "severity": "high",
                            "authority": "llm_provisional",
                            "root_cause_hypothesis": "root cause",
                        },
                        "analysis": {
                            "evidence_bundle_digest": "sha256:evidence",
                            "analyzer": "test",
                            "prompt_version": "1",
                            "candidate_digest": "sha256:candidate",
                        },
                    }
                ],
                "project_sync_status": "completed",
                "batches": ["batch-1"],
            }
        ),
        encoding="utf-8",
    )


def test_evidence_rel_matches_nightly_batch_pattern() -> None:
    assert ADVERSARIAL_YIELD_BATCH_ID == "nightly"
    assert ADVERSARIAL_YIELD_EVIDENCE_REL == "execution/runs/nightly/adversarial-yield.json"


def test_collect_counts_confirmed_and_unclosed_without_problem_link(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce(ce_id="CE-OPEN"))
    _write_ce(
        workspace.change_dir,
        _confirmed_ce(ce_id="CE-LINKED", problem_id="PROB-1"),
    )
    _write_ce(workspace.change_dir, _needs_review_ce())
    _write_campaign(workspace.change_dir, counterexample_count=3, confirmed_count=2)

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )
    assert evidence.status == "evaluated"
    assert evidence.value == 2.0
    assert evidence.counterexample_ids == ("CE-LINKED", "CE-OPEN")
    # A CE cannot close itself by claiming setup.problem_id. Only the canonical
    # issue snapshot Observation -> Occurrence -> Problem join closes it.
    assert evidence.unclosed_count == 2
    assert evidence.sample_count == 3
    assert evidence.seed == SEED
    assert evidence.layer == "api"
    assert evidence.property == "api"
    assert "campaign_result_sha256" in evidence.source
    assert "oracle_set_sha256" in evidence.source or "counterexamples_sha256" in evidence.source


def test_collect_uses_executed_campaign_sample_count_not_counterexample_count(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce())
    _write_campaign(
        workspace.change_dir,
        counterexample_count=1,
        confirmed_count=1,
        sample_count=100,
    )

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )

    assert evidence.status == "evaluated"
    assert evidence.sample_count == 100
    assert evidence.value == 1.0


def test_collect_only_joins_counterexamples_from_current_campaign(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce(ce_id="CE-CURRENT"))
    _write_ce(
        workspace.change_dir,
        _confirmed_ce(ce_id="CE-OLD", campaign_id="CAM-OLD", seed=999),
    )
    _write_campaign(
        workspace.change_dir,
        counterexample_count=1,
        confirmed_count=1,
        sample_count=50,
    )

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )

    assert evidence.status == "evaluated"
    assert evidence.counterexample_ids == ("CE-CURRENT",)
    assert evidence.sample_count == 50
    assert evidence.seed == SEED


def test_foreign_issue_snapshot_cannot_close_current_change_counterexample(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce(ce_id="CE-CURRENT"))
    _write_campaign(workspace.change_dir)
    _write_closing_issue_snapshot(
        workspace.change_dir,
        snapshot_change_id="CH-FOREIGN",
        counterexample_id="CE-CURRENT",
    )

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )

    assert evidence.status == "collection_failed"
    assert any(
        gap.code == "identity_mismatch" and "CH-FOREIGN" in gap.detail for gap in evidence.collection_gaps
    )


def test_replay_summary_ignores_receipts_from_historical_campaigns(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce(ce_id="CE-CURRENT"))
    _write_ce(
        workspace.change_dir,
        _confirmed_ce(ce_id="CE-OLD", campaign_id="CAM-OLD", seed=999),
    )
    _write_campaign(workspace.change_dir, counterexample_count=1, confirmed_count=1)
    write_replay_attempt_receipts(
        workspace.change_dir,
        (
            ReplayAttemptReceipt(
                schema_version="1",
                counterexample_id="CE-CURRENT",
                seed=SEED,
                attempt_index=0,
                base_revision="deadbeef",
                oracle_set_digest="sha256:" + ("a" * 64),
                outcome="violate",
                observed_digest="sha256:" + ("b" * 64),
            ),
            ReplayAttemptReceipt(
                schema_version="1",
                counterexample_id="CE-OLD",
                seed=999,
                attempt_index=0,
                base_revision="deadbeef",
                oracle_set_digest="sha256:" + ("a" * 64),
                outcome="hold",
                observed_digest="sha256:" + ("c" * 64),
            ),
        ),
    )

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )

    assert evidence.seed_replay_attempts == 1
    assert evidence.seed_replay_success == 1
    assert evidence.seed_replay_rate == 1.0


def test_missing_seed_is_identity_mismatch_gap(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    raw = _confirmed_ce()
    del raw["seed"]
    _write_ce(workspace.change_dir, raw)

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )
    assert evidence.status == "collection_failed"
    assert evidence.value is None
    assert any(
        g.code == "identity_mismatch" and g.metric == "adversarial_yield" and "seed" in g.detail
        for g in evidence.collection_gaps
    )


def test_corrupt_ce_yaml_is_artifact_corrupt_not_silent_zero(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    ce_dir = workspace.change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True, exist_ok=True)
    (ce_dir / "CE-BAD.yaml").write_text("{ not: valid: yaml [", encoding="utf-8")

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )
    assert evidence.status == "collection_failed"
    assert evidence.value is None
    assert any(
        g.code == "artifact_corrupt" and g.metric == "adversarial_yield" for g in evidence.collection_gaps
    )


def test_absent_discovery_is_not_evaluated_without_invented_zero(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)

    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=CHANGE_ID,
    )
    assert evidence.status == "not_evaluated"
    assert evidence.value is None
    assert evidence.counterexample_ids == ()
    assert any(b.code == "pending_nightly" and b.metric == "adversarial_yield" for b in evidence.shortboards)
    assert not any(g.code in {"collection_failed", "artifact_corrupt"} for g in evidence.collection_gaps)


def test_operation_writes_evidence_once(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_aa_config(root)
    workspace = _workspace(root)
    _write_ce(workspace.change_dir, _confirmed_ce())
    _write_campaign(workspace.change_dir)

    result = collect_adversarial_yield_operation(_task(), workspace, _context(root))
    assert result.status == "succeeded"
    path = workspace.change_dir / ADVERSARIAL_YIELD_EVIDENCE_REL
    assert path.is_file()
    payload = AdversarialYieldEvidence.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert payload.status == "evaluated"
    assert payload.value == 1.0
    value = result.value
    assert isinstance(value, dict)
    assert value["path"] == ADVERSARIAL_YIELD_EVIDENCE_REL
