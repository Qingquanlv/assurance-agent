"""M3 Task 5 — Issue + workflow end-to-end acceptance (adversarial metrics).

Composes Phase1 campaign/ce_bridge with M3 T1–T4 collectors without inventing a
parallel problem ledger. Scenarios:

A. Happy closed path: discover CE → Problem (existing issues path) → yield clean.
B. Flaky C3 → quarantine → A2 ratio 0.5 → consecutive success → release.
C. Heuristic / needs_review never becomes Problem; yield confirmed count ignores it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.artifacts.models.cases import CaseEntry, CaseRisk
from assurance_agent.artifacts.models.discovery import (
    AdversarialStrategy,
    BudgetBounds,
    CampaignSpec,
    OracleRule,
    OracleSetSnapshot,
    OracleSpec,
    ReplayAttemptReceipt,
    StatusCodeRule,
)
from assurance_agent.artifacts.models.quarantine import QuarantineProjection
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.policy import load_policy_bytes, policy_digest
from assurance_agent.evidence.metrics import aggregate_nightly_metrics
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.evidence.replay_telemetry import compute_seed_replay_rate
from assurance_agent.evidence.risk_tier import RiskTierResolution, resolve_risk_tier
from assurance_agent.verification.oracle import OracleObservation
from assurance_agent.verification.property_scan import PropertyMarkerHit
from assurance_agent.verification.replay import AttemptResult, ReplayAttemptSpec
from assurance_agent.workflow.discovery import run_deterministic_api_campaign
from assurance_agent.workflow.discovery.campaign import (
    CampaignAttemptRunner,
    ExecutionObservation,
    StrategySnapshot,
)
from assurance_agent.workflow.discovery.replay_receipts import (
    load_replay_attempt_receipts,
    write_replay_attempt_receipts,
)
from assurance_agent.workflow.execution.results import PropertyTestResult
from assurance_agent.workflow.metrics.adversarial_yield import collect_adversarial_yield
from assurance_agent.workflow.metrics.constraint_coverage import compute_constraint_coverage
from assurance_agent.workflow.metrics.quarantine import (
    QUARANTINE_PROJECTION_REL,
    load_active_quarantine_keys,
    materialize_quarantine_projection,
)

CHANGE_ID = "CH-M3-ACCEPT"
CAMPAIGN_ID = "CAM-M3-ACCEPT"
SEED = 12345
BASE_REVISION = "deadbeef"
OBSERVED_AT = "2026-08-05T00:00:00Z"
COMPUTED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

KEY_A = "entities.dept.constraints.name_unique"
KEY_B = "entities.dept.constraints.name_has_max_length"

STRONG_API = """
import pytest

@pytest.mark.property("entities.dept.constraints.name_unique")
async def test_a():
    response = {"code": 400, "detail": "exists"}
    assert response["code"] == 400
    assert response["detail"] == "exists"

@pytest.mark.property("entities.dept.constraints.name_has_max_length")
async def test_b():
    response = {"code": 400, "detail": "too long"}
    assert response["code"] == 400
    assert response["detail"] == "too long"
"""


# ---------------------------------------------------------------------------
# Fixtures / fakes (aligned with Phase1 integration slice)
# ---------------------------------------------------------------------------


def _hard_denied_500(oracle_id: str = "ORACLE-status") -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        kind="hard_oracle",
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(denied_codes=(500,))),
    )


def _heuristic_denied_500(oracle_id: str = "ORACLE-heur") -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        kind="search_heuristic",
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(denied_codes=(500,))),
    )


def _strategy() -> AdversarialStrategy:
    return AdversarialStrategy(
        strategy_id="api.status.denied-500",
        version="1",
        surface="api",
        technique="stateful_fuzz",
        oracle_family_ids=("ORACLE-status",),
    )


def _campaign_spec() -> CampaignSpec:
    return CampaignSpec(
        schema_version="1",
        campaign_id=CAMPAIGN_ID,
        change_id=CHANGE_ID,
        surfaces=("api",),
        strategy_ids=("api.status.denied-500",),
        cadence_profile="pr",
        budget=BudgetBounds(max_rounds=1),
        oracle_set_ref="discovery/oracle-set.yaml",
    )


def _oracle_set(*oracles: OracleSpec) -> OracleSetSnapshot:
    return OracleSetSnapshot(
        schema_version="1",
        change_id=CHANGE_ID,
        campaign_id=CAMPAIGN_ID,
        oracles=oracles,
    )


def _strategy_snapshot(*, required: tuple[str, ...] = ("OBL-status-denied",)) -> StrategySnapshot:
    return StrategySnapshot(
        strategies=(_strategy(),),
        required_obligation_ids=required,
    )


def _product_root(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("print('sut')\n", encoding="utf-8")
    (root / "tests" / "api").mkdir(parents=True)
    (root / "tests" / "api" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )
    return root


def _change_dir(project_root: Path) -> Path:
    path = project_root / "qa" / "changes" / CHANGE_ID
    path.mkdir(parents=True)
    return path


@dataclass
class FakeCampaignRunner:
    selection: list[ExecutionObservation]
    replay_results: list[AttemptResult]
    selection_calls: list[dict[str, Any]] = field(default_factory=list)
    replay_calls: list[ReplayAttemptSpec] = field(default_factory=list)
    _replay_index: int = 0

    def run_selection(
        self,
        *,
        workspace: Path,
        manifest: Any,
        seed: int,
        oracle_set_digest: str,
    ) -> list[ExecutionObservation]:
        self.selection_calls.append(
            {
                "workspace": workspace,
                "selection": tuple(manifest.execution_selection),
                "seed": seed,
                "oracle_set_digest": oracle_set_digest,
            }
        )
        return list(self.selection)

    def run_attempt(self, spec: ReplayAttemptSpec) -> AttemptResult:
        self.replay_calls.append(spec)
        if self._replay_index >= len(self.replay_results):
            raise AssertionError("FakeCampaignRunner replay exhausted")
        result = self.replay_results[self._replay_index]
        self._replay_index += 1
        return result


def _violate_obs(
    *,
    oracle_id: str = "ORACLE-status",
    obligation: str = "OBL-status-denied",
    status: int = 500,
) -> ExecutionObservation:
    return ExecutionObservation(
        test_path="tests/api/test_discovery_stub.py",
        oracle_id=oracle_id,
        observation=OracleObservation(status_code=status),
        actions=({"method": "GET", "path": "/api/x"},),
        setup={"tenant": "A"},
        obligation_ids=(obligation,),
    )


def _runner_hard_violate(*, replay_n: int = 3) -> FakeCampaignRunner:
    obs = _violate_obs()
    return FakeCampaignRunner(
        selection=[obs],
        replay_results=[
            AttemptResult(observation=OracleObservation(status_code=500)) for _ in range(replay_n)
        ],
    )


def _critical_risk() -> RiskTierResolution:
    return resolve_risk_tier(
        (
            CaseEntry(
                case_id="TC_API_M3_001",
                title="critical acceptance case",
                status="active",
                priority="P0",
                severity="blocker",
                type="API",
                module="system",
                risk=CaseRisk(level="critical"),
            ),
        )
    )


def _packaged_policy() -> Policy:
    return load_policy_bytes(None, origin="packaged")


def _receipt(
    *,
    ce_id: str,
    outcome: str,
    attempt_index: int,
) -> ReplayAttemptReceipt:
    return ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id=ce_id,
        seed=SEED,
        attempt_index=attempt_index,
        base_revision=BASE_REVISION,
        oracle_set_digest="sha256:" + ("c" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("d" * 64),
    )


def _write_ce_yaml(
    change_dir: Path,
    *,
    ce_id: str,
    obligation_ids: tuple[str, ...],
    finding_status: str = "needs_review",
) -> Path:
    obs = "\n".join(f'  - "{oid}"' for oid in obligation_ids)
    text = f"""schema_version: "1"
counterexample_id: {ce_id}
campaign_id: {CAMPAIGN_ID}
round_id: R1
surface: api
technique: boundary
obligation_ids:
{obs}
oracle_id: ORACLE-1
oracle_kind: hard_oracle
environment_digest: env1
seed: {SEED}
minimization:
  status: minimized
  parent_counterexample_id: null
replay:
  attempts: 2
  reproduced: 1
  artifact_refs: []
finding_status: {finding_status}
"""
    path = change_dir / "discovery" / "counterexamples" / f"{ce_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _hit(key: str, test_name: str) -> PropertyMarkerHit:
    return PropertyMarkerHit(
        file="tests/api/test_props.py",
        test_name=test_name,
        constraint_keys=(key,),
        lineno=4,
    )


def _passed(key: str, test_name: str) -> PropertyTestResult:
    return PropertyTestResult(
        nodeid=f"tests/api/test_props.py::{test_name}",
        file="tests/api/test_props.py",
        constraint_keys=(key,),
        outcome="passed",
        batch_id="20260805-100000",
    )


# ---------------------------------------------------------------------------
# Scenario A — happy closed path
# ---------------------------------------------------------------------------


def test_scenario_a_campaign_to_clean_closed_path(tmp_path: Path) -> None:
    project_root = tmp_path
    change_dir = _change_dir(project_root)
    product = _product_root(tmp_path)
    runner: CampaignAttemptRunner = _runner_hard_violate(replay_n=3)

    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        issues_project_root=project_root,
        runner=runner,
        seed=SEED,
        base_revision=BASE_REVISION,
        replay_attempts=3,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-a",
        observed_at=OBSERVED_AT,
    )
    assert outcome.result.confirmed_count == 1
    assert outcome.counterexample_ids
    ce_id = outcome.counterexample_ids[0]
    ce_path = change_dir / "discovery" / "counterexamples" / f"{ce_id}.yaml"
    assert ce_path.is_file()

    # Replay attempt receipts (C3) from campaign confirm path
    receipts = load_replay_attempt_receipts(change_dir, counterexample_id=ce_id)
    assert len(receipts) == 3
    success, attempts, rate = compute_seed_replay_rate(receipts)
    assert attempts == 3
    assert success == 3
    assert rate == pytest.approx(1.0)

    # Existing Observation → Occurrence → Problem path (no parallel ledger)
    assert outcome.ingest is not None
    assert outcome.ingest.reconcile_status == "completed"
    problems_path = project_root / "qa" / "issues" / "problems.json"
    assert problems_path.is_file()
    problems = json.loads(problems_path.read_text(encoding="utf-8"))
    assert len(problems["problems"]) == 1
    problem_id = problems["problems"][0]["problem_id"]
    assert problem_id.startswith("PROB-")

    snapshot = json.loads((change_dir / "issues" / "snapshot.json").read_text(encoding="utf-8"))
    assert len(snapshot["observations"]) == 1
    assert len(snapshot["occurrences"]) == 1
    assert snapshot["occurrences"][0]["problem_id"] == problem_id

    # No second / discovery-local problem ledger
    assert not (change_dir / "discovery" / "problems.json").exists()
    assert not (change_dir / "discovery" / "problem-ledger.json").exists()
    assert list((project_root / "qa" / "issues").glob("**/problems.json")) == [problems_path]

    policy = _packaged_policy()
    digest = policy_digest(policy)
    risk = _critical_risk()
    assert risk.tier == "critical"

    # The canonical Observation → Occurrence → Problem join is the closure
    # signal. Counterexample source artifacts remain immutable.
    closed_yield = collect_adversarial_yield(change_dir=change_dir, change_id=CHANGE_ID)
    assert closed_yield.status == "evaluated"
    assert closed_yield.value == pytest.approx(1.0)
    assert closed_yield.unclosed_count == 0
    assert closed_yield.seed_replay_rate == pytest.approx(1.0)

    closed_doc = aggregate_nightly_metrics(
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        policy_digest=digest,
        risk=risk,
        mutation=None,
        assertion_strength=None,
        baseline_drift=None,
        adversarial_yield=closed_yield,
        floors=policy.evidence_sufficiency.floors["critical"],
        sufficiency=policy.evidence_sufficiency,
    )
    assert closed_doc.metrics["adversarial_yield"].status == "evaluated"
    assert closed_doc.metrics["adversarial_clean"].holds is True
    closed_decision = evaluate_metrics_sufficiency(closed_doc, policy.evidence_sufficiency)
    assert closed_decision.verdict == "pass"


# ---------------------------------------------------------------------------
# Scenario B — flaky → quarantine → A2 exclusion → release
# ---------------------------------------------------------------------------


def test_scenario_b_flaky_quarantine_a2_exclusion_and_release(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    ce_id = "CE-FLAKY-M3"
    _write_ce_yaml(change_dir, ce_id=ce_id, obligation_ids=(KEY_B,))

    write_replay_attempt_receipts(
        change_dir,
        (
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=0),
            _receipt(ce_id=ce_id, outcome="hold", attempt_index=1),
        ),
    )
    success, attempts, rate = compute_seed_replay_rate(load_replay_attempt_receipts(change_dir))
    assert attempts == 2
    assert success == 1
    assert rate is not None and rate < 1.0

    projection = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id=CHANGE_ID,
        entered_at="2026-08-05T12:00:00Z",
    )
    assert any(e.subject_key == KEY_B and e.status == "active" for e in projection.entries)
    assert (change_dir / QUARANTINE_PROJECTION_REL).is_file()
    active = load_active_quarantine_keys(change_dir)
    assert KEY_B in active

    # A2: one of two keys quarantined → 0.5, never 1.0 by dropping denom
    evidence = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id="20260805-100000",
        known_keys=frozenset({KEY_A, KEY_B}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            _passed(KEY_A, "test_a"),
            _passed(KEY_B, "test_b"),
        ),
        marker_hits=(_hit(KEY_A, "test_a"), _hit(KEY_B, "test_b")),
        test_sources={"tests/api/test_props.py": STRONG_API},
        quarantine=tuple(sorted(active)),
    )
    assert evidence.declared is not None
    assert evidence.declared.total == 2
    assert evidence.declared.covered == 1
    assert evidence.value == pytest.approx(0.5)
    assert KEY_B in evidence.declared.uncovered

    # Consecutive success receipts → release → covered allowed again
    write_replay_attempt_receipts(
        change_dir,
        (
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=2),
            _receipt(ce_id=ce_id, outcome="violate", attempt_index=3),
        ),
    )
    released = materialize_quarantine_projection(
        change_dir=change_dir,
        change_id=CHANGE_ID,
        entered_at="2026-08-05T13:00:00Z",
    )
    entry = next(e for e in released.entries if e.subject_key == KEY_B)
    assert entry.status == "released"
    assert KEY_B not in load_active_quarantine_keys(change_dir)

    after = compute_constraint_coverage(
        change_id=CHANGE_ID,
        batch_id="20260805-100000",
        known_keys=frozenset({KEY_A, KEY_B}),
        touched_entities=frozenset({"dept"}),
        property_tests=(
            _passed(KEY_A, "test_a"),
            _passed(KEY_B, "test_b"),
        ),
        marker_hits=(_hit(KEY_A, "test_a"), _hit(KEY_B, "test_b")),
        test_sources={"tests/api/test_props.py": STRONG_API},
        quarantine=tuple(sorted(load_active_quarantine_keys(change_dir))),
    )
    assert after.declared is not None
    assert after.declared.total == 2
    assert after.declared.covered == 2
    assert after.value == pytest.approx(1.0)

    loaded = QuarantineProjection.model_validate_json(
        (change_dir / QUARANTINE_PROJECTION_REL).read_text(encoding="utf-8")
    )
    assert loaded.change_id == CHANGE_ID


# ---------------------------------------------------------------------------
# Scenario C — heuristic / needs_review ignored by Problem + confirmed yield
# ---------------------------------------------------------------------------


def test_scenario_c_heuristic_cannot_become_problem_or_confirmed_yield(tmp_path: Path) -> None:
    project_root = tmp_path
    change_dir = _change_dir(project_root)
    product = _product_root(tmp_path)
    runner = FakeCampaignRunner(
        selection=[_violate_obs(oracle_id="ORACLE-heur", obligation="OBL-heur")],
        replay_results=[AttemptResult(observation=OracleObservation(status_code=500)) for _ in range(3)],
    )
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_heuristic_denied_500()),
        strategy_snapshot=_strategy_snapshot(required=("OBL-heur",)),
        change_dir=change_dir,
        project_root=product,
        issues_project_root=project_root,
        runner=runner,
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-c",
        observed_at=OBSERVED_AT,
    )
    assert outcome.result.confirmed_count == 0
    assert not (project_root / "qa" / "issues" / "problems.json").is_file()

    ce_dir = change_dir / "discovery" / "counterexamples"
    if ce_dir.is_dir():
        for path in ce_dir.glob("*.yaml"):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert payload["finding_status"] != "confirmed"

    # A heuristic observation does not manufacture a confirmed CE; the campaign
    # receipt still records the executed sample so zero yield is honest.
    assert outcome.result.sample_count == 1
    yield_ev = collect_adversarial_yield(change_dir=change_dir, change_id=CHANGE_ID)
    assert yield_ev.status == "evaluated"
    assert yield_ev.value == pytest.approx(0.0)
    assert yield_ev.counterexample_ids == ()
    assert yield_ev.unclosed_count == 0
