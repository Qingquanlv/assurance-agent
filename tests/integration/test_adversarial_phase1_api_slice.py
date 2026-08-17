"""Adversarial Discovery Phase 1 — e2e acceptance (Task E).

Covers design §15 items 1–5, 7–8, 14 via the deterministic fallback controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import (
    AdversarialStrategy,
    BudgetBounds,
    CampaignSpec,
    GeneratedFileEntry,
    GeneratedManifest,
    OracleRule,
    OracleSetSnapshot,
    OracleSpec,
    StatusCodeRule,
)
from assurance_agent.verification.manifest import ManifestValidationError, validate_generated_manifest
from assurance_agent.verification.oracle import (
    OracleObservation,
    evaluate_frozen_oracles,
    evaluate_oracle,
)
from assurance_agent.verification.replay import AttemptResult, ReplayAttemptSpec
from assurance_agent.workflow.discovery import run_deterministic_api_campaign
from assurance_agent.workflow.discovery.campaign import (
    CampaignAttemptRunner,
    CampaignError,
    ExecutionObservation,
    StrategySnapshot,
)


CHANGE_ID = "CH-ADV-P1"
CAMPAIGN_ID = "CAM-ADV-P1"
SEED = 12345
BASE_REVISION = "deadbeef"


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


def _strategy(*, obligation: str = "OBL-status-denied") -> AdversarialStrategy:
    return AdversarialStrategy(
        strategy_id="api.status.denied-500",
        version="1",
        surface="api",
        technique="stateful_fuzz",
        oracle_family_ids=("ORACLE-status",),
    )


def _campaign_spec(*, max_rounds: int = 1) -> CampaignSpec:
    return CampaignSpec(
        schema_version="1",
        campaign_id=CAMPAIGN_ID,
        change_id=CHANGE_ID,
        surfaces=("api",),
        strategy_ids=("api.status.denied-500",),
        cadence_profile="pr",
        budget=BudgetBounds(max_rounds=max_rounds),
        oracle_set_ref="discovery/oracle-set.yaml",
    )


def _oracle_set(*oracles: OracleSpec) -> OracleSetSnapshot:
    return OracleSetSnapshot(
        schema_version="1",
        change_id=CHANGE_ID,
        campaign_id=CAMPAIGN_ID,
        oracles=oracles,
    )


def _strategy_snapshot(
    *,
    required: tuple[str, ...] = ("OBL-status-denied",),
) -> StrategySnapshot:
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


def _change_dir(tmp_path: Path) -> Path:
    path = tmp_path / "qa" / "changes" / CHANGE_ID
    path.mkdir(parents=True)
    return path


@dataclass
class FakeCampaignRunner:
    """Injectable runner: fixed selection observations + replay results."""

    selection: list[ExecutionObservation]
    replay_results: list[AttemptResult]
    selection_calls: list[dict[str, Any]] = field(default_factory=list)
    replay_calls: list[ReplayAttemptSpec] = field(default_factory=list)
    _replay_index: int = 0

    def run_selection(
        self,
        *,
        workspace: Path,
        manifest: GeneratedManifest,
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


def _hold_obs(*, obligation: str = "OBL-status-denied") -> ExecutionObservation:
    return ExecutionObservation(
        test_path="tests/api/test_discovery_stub.py",
        oracle_id="ORACLE-status",
        observation=OracleObservation(status_code=200),
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


# ---------------------------------------------------------------------------
# Acceptance matrix
# ---------------------------------------------------------------------------


def test_1_hard_oracle_violation_writes_counterexample(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-1",
        observed_at="2026-08-05T00:00:00Z",
    )
    assert outcome.result.confirmed_count >= 1
    assert outcome.counterexample_ids
    ce_path = change_dir / "discovery" / "counterexamples" / f"{outcome.counterexample_ids[0]}.json"
    assert ce_path.is_file()
    payload = json.loads(ce_path.read_text(encoding="utf-8"))
    assert payload["finding_status"] == "confirmed"
    assert payload["oracle_kind"] == "hard_oracle"
    assert payload["replay"]["reproduced"] == payload["replay"]["attempts"]


def test_2_frozen_oracle_same_observation_same_verdict() -> None:
    oracle = _hard_denied_500()
    obs = OracleObservation(status_code=500)
    a = evaluate_oracle(oracle, obs)
    b = evaluate_oracle(oracle, obs)
    assert a == b
    frozen = evaluate_frozen_oracles(
        _oracle_set(oracle),
        (("ORACLE-status", obs), ("ORACLE-status", obs)),
    )
    assert frozen[0].verdict == frozen[1].verdict == a


def test_3_deterministic_replay_full_reproduce_confirmed(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(replay_n=3),
        seed=SEED,
        base_revision=BASE_REVISION,
        replay_attempts=3,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-replay",
        observed_at="2026-08-05T00:00:00Z",
    )
    assert outcome.result.confirmed_count == 1
    ce_id = outcome.counterexample_ids[0]
    payload = json.loads(
        (change_dir / "discovery" / "counterexamples" / f"{ce_id}.json").read_text(encoding="utf-8")
    )
    assert payload["replay"]["attempts"] == 3
    assert payload["replay"]["reproduced"] == 3
    assert payload["finding_status"] == "confirmed"


def test_4_ce_to_problem_full_chain(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    # Project root must contain product tree AND be the qa issues parent.
    project_root = tmp_path
    product = _product_root(tmp_path)
    # materialize copies from product; issues land under project_root/qa/issues
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        issues_project_root=project_root,
        runner=_runner_hard_violate(),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-problem",
        observed_at="2026-08-05T00:00:00Z",
    )
    assert outcome.ingest is not None
    assert outcome.ingest.observation_count == 1
    assert outcome.ingest.reconcile_status == "completed"
    problems = json.loads((project_root / "qa" / "issues" / "problems.json").read_text(encoding="utf-8"))
    assert len(problems["problems"]) == 1
    assert problems["problems"][0]["assessment"]["classification"] == "product_bug"


def test_5_path_traversal_rejected_at_manifest_validate(tmp_path: Path) -> None:
    round_dir = tmp_path / "rounds" / "R0001"
    (round_dir / "generated" / "tests" / "api").mkdir(parents=True)
    body = b"def test_x():\n    assert True\n"
    (round_dir / "generated" / "tests" / "api" / "test_x.py").write_bytes(body)
    with pytest.raises(ValidationError):
        GeneratedFileEntry(
            source="generated/../secret.py",
            target="tests/api/test_x.py",
            sha256=sha256_bytes(body),
            role="search_test",
        )
    # Also reject via on-disk validate when model somehow bypassed (symlink escape covered in unit).
    safe = GeneratedManifest(
        schema_version="1",
        change_id=CHANGE_ID,
        campaign_id=CAMPAIGN_ID,
        round_id="R0001",
        parent_round_ids=(),
        base_revision=BASE_REVISION,
        strategy_ids=("api.status.denied-500",),
        files=(
            GeneratedFileEntry(
                source="generated/tests/api/test_x.py",
                target="tests/api/test_x.py",
                sha256=sha256_bytes(body),
                role="search_test",
            ),
        ),
        execution_selection=("tests/api/test_x.py",),
        oracle_refs=("ORACLE-status",),
        seed=SEED,
    )
    # Happy validate still works for safe paths.
    validate_generated_manifest(round_dir, safe)

    # Extra unmanifested file under generated/ is rejected.
    (round_dir / "generated" / "evil.py").write_text("x", encoding="utf-8")
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_dir, safe)
    assert exc.value.code == "extra_unmanifested_file"


def test_6_temp_destroyed_discovery_assets_persist(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    ws = tmp_path / "workspaces" / "ws-persist"
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: ws,
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    assert outcome.workspace_destroyed is True
    assert not ws.exists()
    assert (change_dir / "discovery" / "rounds" / "R0001" / "decision.json").is_file()
    assert (change_dir / "discovery" / "rounds" / "R0001" / "generated-manifest.json").is_file()
    assert (
        change_dir
        / "discovery"
        / "rounds"
        / "R0001"
        / "generated"
        / "tests"
        / "api"
        / "test_discovery_stub.py"
    ).is_file()
    assert (change_dir / "discovery" / "campaign-result.json").is_file()
    assert list((change_dir / "discovery" / "counterexamples").glob("*.json"))


def test_7_heuristic_cannot_become_problem(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    project_root = tmp_path
    runner = FakeCampaignRunner(
        selection=[
            _violate_obs(oracle_id="ORACLE-heur", obligation="OBL-heur"),
        ],
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
        temp_factory=lambda: tmp_path / "workspaces" / "ws-heur",
        observed_at="2026-08-05T00:00:00Z",
    )
    assert outcome.result.confirmed_count == 0
    ce_dir = change_dir / "discovery" / "counterexamples"
    if ce_dir.is_dir():
        for path in ce_dir.glob("*.yaml"):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert payload["finding_status"] != "confirmed"
            assert payload["oracle_kind"] != "hard_oracle" or payload["finding_status"] != "confirmed"
    assert not (project_root / "qa" / "issues" / "problems.json").is_file()


def test_8_identical_seed_snapshot_equivalent_digests(tmp_path: Path) -> None:
    def _run(label: str) -> Any:
        root = tmp_path / label
        change_dir = root / "qa" / "changes" / CHANGE_ID
        change_dir.mkdir(parents=True)
        product = _product_root(root)
        return run_deterministic_api_campaign(
            campaign_spec=_campaign_spec(),
            oracle_set=_oracle_set(_hard_denied_500()),
            strategy_snapshot=_strategy_snapshot(),
            change_dir=change_dir,
            project_root=product,
            runner=_runner_hard_violate(),
            seed=SEED,
            base_revision=BASE_REVISION,
            model_available=False,
            temp_factory=lambda: root / "workspaces" / "ws",
            observed_at="2026-08-05T00:00:00Z",
            ingest=False,
        )

    a = _run("a")
    b = _run("b")
    assert a.result_digest == b.result_digest
    assert a.counterexample_ids == b.counterexample_ids
    assert a.result == b.result


def test_9_fallback_is_only_phase1_path(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    # model unavailable → deterministic fallback succeeds
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(),
        change_dir=change_dir,
        project_root=product,
        runner=_runner_hard_violate(),
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-fb",
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    assert outcome.result.rounds_completed == 1
    assert outcome.result.status in {"completed", "stopped_budget", "stopped_fail_fast"}

    # model available → Phase 1 refuses LLM controller (fallback is the only path)
    with pytest.raises(CampaignError) as exc:
        run_deterministic_api_campaign(
            campaign_spec=_campaign_spec(),
            oracle_set=_oracle_set(_hard_denied_500()),
            strategy_snapshot=_strategy_snapshot(),
            change_dir=change_dir,
            project_root=product,
            runner=_runner_hard_violate(),
            seed=SEED,
            model_available=True,
        )
    assert exc.value.code == "llm_controller_out_of_scope"


def test_missing_required_obligation_not_ordinary_pass(tmp_path: Path) -> None:
    """§15.8 — uncovered required obligations must not look like clean success."""
    change_dir = _change_dir(tmp_path)
    product = _product_root(tmp_path)
    runner = FakeCampaignRunner(
        selection=[_hold_obs(obligation="OBL-other")],
        replay_results=[],
    )
    outcome = run_deterministic_api_campaign(
        campaign_spec=_campaign_spec(),
        oracle_set=_oracle_set(_hard_denied_500()),
        strategy_snapshot=_strategy_snapshot(required=("OBL-status-denied",)),
        change_dir=change_dir,
        project_root=product,
        runner=runner,
        seed=SEED,
        base_revision=BASE_REVISION,
        model_available=False,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-obl",
        observed_at="2026-08-05T00:00:00Z",
        ingest=False,
    )
    assert outcome.result.status != "completed" or outcome.result.stop_reason is not None
    assert outcome.result.status in {"failed", "stopped_budget", "stopped_fail_fast"}
    assert outcome.result.stop_reason is not None
    assert "obligation" in outcome.result.stop_reason.lower()


def test_protocol_surface_accepts_fake_runner() -> None:
    runner: CampaignAttemptRunner = _runner_hard_violate()
    assert hasattr(runner, "run_selection")
    assert hasattr(runner, "run_attempt")
