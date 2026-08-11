"""CE → Observation → Occurrence → Problem bridge (Phase 1, Task D)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.discovery import (
    Counterexample,
    CounterexampleReplay,
    MinimizationInfo,
)
from assurance_agent.artifacts.models.issues import Observation
from assurance_agent.workflow.discovery.ce_bridge import (
    CeBridgeError,
    CeIngestResult,
    confirmed_ces_to_candidate_document,
    counterexample_to_observation,
    ingest_confirmed_counterexamples,
)


def _replay(**overrides: Any) -> CounterexampleReplay:
    base: dict[str, Any] = dict(attempts=3, reproduced=3, artifact_refs=("execution/replay-1.json",))
    base.update(overrides)
    return CounterexampleReplay(**base)


def _counterexample(**overrides: Any) -> Counterexample:
    base: dict[str, Any] = dict(
        schema_version="1",
        counterexample_id="CE-auth-tenant-001",
        campaign_id="CAM-001",
        round_id="R0007",
        surface="api",
        technique="stateful_fuzz",
        obligation_ids=("OBL-auth-tenant-read",),
        oracle_id="ORACLE-auth-tenant-isolation",
        oracle_kind="hard_oracle",
        environment_digest="sha256:" + ("a" * 64),
        generated_file_digests={"generated/tests/api/test_tenant.py": "sha256:" + ("b" * 64)},
        setup={"tenant": "A"},
        actions=({"method": "GET", "path": "/api/users"},),
        observed={"status": 200},
        expected={"status": 403},
        seed=12345,
        minimization=MinimizationInfo(status="minimized", parent_counterexample_id=None),
        replay=_replay(),
        finding_status="confirmed",
    )
    base.update(overrides)
    return Counterexample(**base)


def _write_ce(change_dir: Path, ce: Counterexample) -> Path:
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True, exist_ok=True)
    path = ce_dir / f"{ce.counterexample_id}.yaml"
    path.write_text(
        yaml.safe_dump(ce.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Pure bridge: Observation identity
# ---------------------------------------------------------------------------


def test_confirmed_ce_to_observation_stable_id() -> None:
    ce = _counterexample()
    obs_a = counterexample_to_observation(
        ce,
        change_id="CH-DEMO-001",
        batch_id="DISC-BATCH-001",
        observed_at="2026-08-05T00:00:00Z",
    )
    obs_b = counterexample_to_observation(
        ce,
        change_id="CH-DEMO-001",
        batch_id="DISC-BATCH-001",
        observed_at="2026-08-05T12:00:00Z",
    )
    assert isinstance(obs_a, Observation)
    assert obs_a.observation_id.startswith("OBS-")
    assert obs_a.observation_id == obs_b.observation_id
    assert obs_a.kind == "anomaly"
    assert obs_a.target == "api"
    assert obs_a.evidence_refs == ["discovery/counterexamples/CE-auth-tenant-001.yaml"]
    assert obs_a.source.artifact == "discovery/counterexamples/CE-auth-tenant-001.yaml"
    assert obs_a.signature
    assert obs_a.observed_at == "2026-08-05T00:00:00Z"


def test_signature_deterministic_from_ce_identity() -> None:
    ce_a = _counterexample()
    ce_b = _counterexample(round_id="R9999", observed={"status": 201})
    obs_a = counterexample_to_observation(
        ce_a, change_id="CH-1", batch_id="B-1", observed_at="2026-08-05T00:00:00Z"
    )
    obs_b = counterexample_to_observation(
        ce_b, change_id="CH-1", batch_id="B-1", observed_at="2026-08-05T00:00:00Z"
    )
    assert obs_a.signature == obs_b.signature

    ce_c = _counterexample(seed=99999)
    obs_c = counterexample_to_observation(
        ce_c, change_id="CH-1", batch_id="B-1", observed_at="2026-08-05T00:00:00Z"
    )
    assert obs_c.signature != obs_a.signature


def test_search_heuristic_cannot_be_confirmed() -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        _counterexample(oracle_kind="search_heuristic", finding_status="confirmed")


def test_confirmed_ces_to_candidate_document_provisional_product_bug() -> None:
    ce = _counterexample()
    obs = counterexample_to_observation(
        ce, change_id="CH-1", batch_id="B-1", observed_at="2026-08-05T00:00:00Z"
    )
    digest = "sha256:" + ("c" * 64)
    doc = confirmed_ces_to_candidate_document(
        [ce],
        [obs],
        change_id="CH-1",
        batch_id="B-1",
        evidence_bundle_digest=digest,
    )
    assert doc.schema_version == "1.0"
    assert doc.evidence_bundle_digest == digest
    assert len(doc.candidates) == 1
    cand = doc.candidates[0]
    assert cand.observation_ids == [obs.observation_id]
    assert cand.proposed.classification == "product_bug"
    assert cand.proposed.severity in {"critical", "high", "medium", "low"}
    assert cand.affected_surface.kind == "endpoint"
    assert "GET" in cand.affected_surface.value
    assert cand.fingerprint_inputs.symptom
    assert cand.possible_problem_ids == []
    assert 0.0 <= cand.confidence <= 1.0


# ---------------------------------------------------------------------------
# Ingest + reconcile
# ---------------------------------------------------------------------------


def test_needs_review_ce_skips_observation_and_problem(tmp_path: Path) -> None:
    change_id = "CH-needs-review"
    change_dir = tmp_path / "qa" / "changes" / change_id
    project_root = tmp_path
    ce = _counterexample(
        finding_status="needs_review",
        oracle_kind="hard_oracle",
        replay=_replay(attempts=1, reproduced=0),
    )
    _write_ce(change_dir, ce)

    result = ingest_confirmed_counterexamples(
        change_dir,
        change_id=change_id,
        batch_id="DISC-BATCH-NR",
        project_root=project_root,
        reconcile=True,
        observed_at="2026-08-05T00:00:00Z",
    )
    assert isinstance(result, CeIngestResult)
    assert result.observation_count == 0
    assert result.candidate_count == 0
    assert result.skipped_non_confirmed == 1

    obs_path = change_dir / "inspect" / "observations.json"
    assert obs_path.is_file()
    obs_doc = json.loads(obs_path.read_text(encoding="utf-8"))
    assert obs_doc["observations"] == []

    problems_path = project_root / "qa" / "issues" / "problems.json"
    if problems_path.is_file():
        problems = json.loads(problems_path.read_text(encoding="utf-8"))
        assert problems["problems"] == []


def test_inconclusive_evidence_ce_skips_problem(tmp_path: Path) -> None:
    change_id = "CH-inconclusive"
    change_dir = tmp_path / "qa" / "changes" / change_id
    project_root = tmp_path
    ce = _counterexample(
        counterexample_id="CE-inconclusive-001",
        finding_status="inconclusive_evidence",
        replay=_replay(attempts=2, reproduced=1),
    )
    _write_ce(change_dir, ce)

    result = ingest_confirmed_counterexamples(
        change_dir,
        change_id=change_id,
        batch_id="DISC-BATCH-IE",
        project_root=project_root,
        reconcile=True,
        observed_at="2026-08-05T00:00:00Z",
    )
    assert result.observation_count == 0
    assert result.candidate_count == 0
    assert result.skipped_non_confirmed == 1


def test_invalid_ce_yaml_is_typed_failure_not_silent_skip(tmp_path: Path) -> None:
    change_id = "CH-bad-ce"
    change_dir = tmp_path / "qa" / "changes" / change_id
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True, exist_ok=True)
    (ce_dir / "broken.yaml").write_text("finding_status: confirmed\nnot: a valid ce\n", encoding="utf-8")

    with pytest.raises(CeBridgeError) as exc_info:
        ingest_confirmed_counterexamples(
            change_dir,
            change_id=change_id,
            batch_id="DISC-BATCH-BAD",
            project_root=tmp_path,
            reconcile=False,
        )
    assert exc_info.value.code in {
        "invalid_counterexample",
        "incomplete_evidence",
        "unreadable_counterexample",
    }


def test_confirmed_search_heuristic_yaml_is_typed_failure(tmp_path: Path) -> None:
    """Model forbids confirmed+search_heuristic; ingest must not skip silently."""
    change_id = "CH-heuristic"
    change_dir = tmp_path / "qa" / "changes" / change_id
    ce_dir = change_dir / "discovery" / "counterexamples"
    ce_dir.mkdir(parents=True, exist_ok=True)
    payload = _counterexample().model_dump(mode="json")
    payload["oracle_kind"] = "search_heuristic"
    payload["finding_status"] = "confirmed"
    (ce_dir / "CE-bad.yaml").write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(CeBridgeError) as exc_info:
        ingest_confirmed_counterexamples(
            change_dir,
            change_id=change_id,
            batch_id="DISC-BATCH-H",
            project_root=tmp_path,
            reconcile=False,
        )
    assert exc_info.value.code == "invalid_counterexample"


def test_end_to_end_confirmed_ce_produces_problem(tmp_path: Path) -> None:
    change_id = "CH-ce-e2e"
    change_dir = tmp_path / "qa" / "changes" / change_id
    project_root = tmp_path
    ce = _counterexample()
    ce_path = _write_ce(change_dir, ce)

    result = ingest_confirmed_counterexamples(
        change_dir,
        change_id=change_id,
        batch_id="DISC-BATCH-E2E",
        project_root=project_root,
        reconcile=True,
        observed_at="2026-08-05T00:00:00Z",
    )
    assert result.observation_count == 1
    assert result.candidate_count == 1
    assert result.evidence_bundle_digest.startswith("sha256:")

    obs_doc = json.loads((change_dir / "inspect" / "observations.json").read_text(encoding="utf-8"))
    assert len(obs_doc["observations"]) == 1
    obs = obs_doc["observations"][0]
    assert obs["evidence_refs"] == ["discovery/counterexamples/CE-auth-tenant-001.yaml"]
    assert ce_path.is_file()

    manifest = json.loads(
        (change_dir / "inspect" / "issue-evidence-manifest.json").read_text(encoding="utf-8")
    )
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "discovery/counterexamples/CE-auth-tenant-001.yaml" in paths
    assert any(
        entry["path"] == "discovery/counterexamples/CE-auth-tenant-001.yaml"
        and entry["digest"].startswith("sha256:")
        for entry in manifest["entries"]
    )

    snapshot = json.loads((change_dir / "issues" / "snapshot.json").read_text(encoding="utf-8"))
    assert len(snapshot["observations"]) == 1
    assert len(snapshot["occurrences"]) == 1
    assert snapshot["occurrences"][0]["provisional_assessment"]["classification"] == "product_bug"
    assert snapshot["occurrences"][0]["provisional_assessment"]["authority"] == "llm_provisional"

    problems = json.loads((project_root / "qa" / "issues" / "problems.json").read_text(encoding="utf-8"))
    assert len(problems["problems"]) == 1
    problem = problems["problems"][0]
    assert problem["problem_id"].startswith("PROB-")
    assert problem["assessment"]["classification"] == "product_bug"
    assert problem["assessment"]["authority"] == "llm_provisional"
    assert problem["status"] == "detected"


def test_ingest_without_reconcile_writes_candidates_only(tmp_path: Path) -> None:
    change_id = "CH-no-rec"
    change_dir = tmp_path / "qa" / "changes" / change_id
    _write_ce(change_dir, _counterexample())

    result = ingest_confirmed_counterexamples(
        change_dir,
        change_id=change_id,
        batch_id="DISC-BATCH-NR2",
        project_root=tmp_path,
        reconcile=False,
        observed_at="2026-08-05T00:00:00Z",
    )
    assert result.candidate_count == 1
    assert (change_dir / "inspect" / "issue-candidates.json").is_file()
    assert not (tmp_path / "qa" / "issues" / "problems.json").is_file()
