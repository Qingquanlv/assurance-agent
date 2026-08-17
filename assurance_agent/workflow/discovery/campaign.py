"""Deterministic fallback campaign runner (Phase 1 API vertical slice).

Phase 1 controller = deterministic fallback only. Full adaptive LLM controller
is out of scope: callers must pass ``model_available=False``. The runner
composes existing pure APIs (manifest validate, materialize, frozen oracle,
replay/confirm, CE ingest) into one Change-local campaign loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import (
    AdversarialStrategy,
    CampaignResult,
    CampaignSpec,
    CampaignStatus,
    Counterexample,
    CounterexampleReplay,
    GeneratedFileEntry,
    GeneratedManifest,
    MinimizationInfo,
    OracleSetSnapshot,
    RoundDecision,
)
from assurance_agent.verification.manifest import digest_bytes, validate_generated_manifest
from assurance_agent.verification.oracle import (
    OracleObservation,
    confirm_counterexample,
    evaluate_frozen_oracles,
)
from assurance_agent.verification.replay import (
    AttemptResult,
    ReplayAttemptSpec,
    build_replay_attempt_receipts,
    replay_counterexample,
)
from assurance_agent.workflow.discovery.ce_bridge import CeIngestResult, ingest_confirmed_counterexamples
from assurance_agent.workflow.discovery.materialize import destroy_workspace, materialize_round
from assurance_agent.workflow.discovery.replay_receipts import write_replay_attempt_receipts

_STUB_REL = "generated/tests/api/test_discovery_stub.py"
_STUB_TARGET = "tests/api/test_discovery_stub.py"
_DEFAULT_REPLAY_ATTEMPTS = 3


class CampaignError(Exception):
    """Typed campaign failure (fail-closed controller policy)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StrategySnapshot:
    """Minimal strategy snapshot consumed by the Phase 1 fallback controller."""

    strategies: tuple[AdversarialStrategy, ...]
    required_obligation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionObservation:
    """Structured observation produced by executing a selected generated test."""

    test_path: str
    oracle_id: str
    observation: OracleObservation
    actions: tuple[dict[str, Any], ...] = ()
    setup: dict[str, Any] = field(default_factory=dict)
    obligation_ids: tuple[str, ...] = ()


class CampaignAttemptRunner(Protocol):
    """Injectable selection executor + replay AttemptRunner."""

    def run_selection(
        self,
        *,
        workspace: Path,
        manifest: GeneratedManifest,
        seed: int,
        oracle_set_digest: str,
    ) -> Sequence[ExecutionObservation]: ...

    def run_attempt(self, spec: ReplayAttemptSpec) -> AttemptResult: ...


@dataclass(frozen=True)
class CampaignOutcome:
    """In-memory outcome of one deterministic campaign run."""

    result: CampaignResult
    result_digest: str
    counterexample_ids: tuple[str, ...]
    ingest: CeIngestResult | None
    round_ids: tuple[str, ...]
    workspace_destroyed: bool


def run_deterministic_api_campaign(
    *,
    campaign_spec: CampaignSpec,
    oracle_set: OracleSetSnapshot,
    strategy_snapshot: StrategySnapshot,
    change_dir: Path,
    project_root: Path,
    runner: CampaignAttemptRunner,
    seed: int,
    base_revision: str = "phase1-fixture",
    replay_attempts: int = _DEFAULT_REPLAY_ATTEMPTS,
    preauthored_generated: Mapping[str, bytes] | None = None,
    model_available: bool = False,
    temp_factory: Callable[[], Path] | None = None,
    ingest: bool = True,
    issues_project_root: Path | None = None,
    batch_id: str | None = None,
    observed_at: str | None = None,
) -> CampaignOutcome:
    """Run the Phase 1 deterministic fallback campaign (API surface only).

    ``model_available=True`` is rejected: the adaptive LLM controller is out of
    scope for Phase 1. Missing required obligation evidence never yields a clean
    ``completed`` success — status is ``failed`` / ``stopped_*`` with a structured
    ``stop_reason``.
    """
    if model_available:
        raise CampaignError(
            "llm_controller_out_of_scope",
            "Phase 1 supports only deterministic fallback (model unavailable)",
        )
    if "api" not in campaign_spec.surfaces:
        raise CampaignError("surface_not_supported", "Phase 1 campaign requires api surface")
    if campaign_spec.change_id != oracle_set.change_id:
        raise CampaignError("change_id_mismatch", "campaign_spec.change_id != oracle_set.change_id")
    if campaign_spec.campaign_id != oracle_set.campaign_id:
        raise CampaignError(
            "campaign_id_mismatch",
            "campaign_spec.campaign_id != oracle_set.campaign_id",
        )
    if not strategy_snapshot.strategies:
        raise CampaignError("empty_strategy_snapshot", "strategy_snapshot.strategies is empty")

    change_dir = Path(change_dir)
    project_root = Path(project_root)
    discovery_dir = change_dir / "discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)

    _write_json(discovery_dir / "campaign-spec.json", campaign_spec.model_dump(mode="json"))
    _write_json(discovery_dir / "oracle-set.json", oracle_set.model_dump(mode="json"))
    _write_yaml(
        discovery_dir / "strategy-snapshot.yaml",
        {
            "strategies": [s.model_dump(mode="json") for s in strategy_snapshot.strategies],
            "required_obligation_ids": list(strategy_snapshot.required_obligation_ids),
        },
    )

    oracle_set_digest = sha256_bytes(
        json.dumps(oracle_set.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    )
    max_rounds = _max_rounds(campaign_spec)
    strategy_ids = tuple(s.strategy_id for s in strategy_snapshot.strategies)
    technique = strategy_snapshot.strategies[0].technique

    confirmed_ids: list[str] = []
    all_ce_ids: list[str] = []
    covered_obligations: set[str] = set()
    rounds_completed = 0
    round_ids: list[str] = []
    workspace_destroyed = True
    last_workspace: Path | None = None
    sample_count = 0

    try:
        for round_index in range(1, max_rounds + 1):
            round_id = f"R{round_index:04d}"
            round_ids.append(round_id)
            round_dir = discovery_dir / "rounds" / round_id
            round_dir.mkdir(parents=True, exist_ok=True)

            decision = RoundDecision(
                schema_version="1",
                change_id=campaign_spec.change_id,
                campaign_id=campaign_spec.campaign_id,
                round_id=round_id,
                parent_round_ids=tuple(round_ids[:-1]),
                strategy_ids=strategy_ids,
                seed=seed,
                parameters={
                    "controller": "deterministic_fallback",
                    "model_available": False,
                    "round_index": round_index,
                },
            )
            _write_json(round_dir / "decision.json", decision.model_dump(mode="json"))

            manifest = _prepare_generated_tree(
                round_dir=round_dir,
                campaign_spec=campaign_spec,
                round_id=round_id,
                strategy_ids=strategy_ids,
                seed=seed,
                base_revision=base_revision,
                oracle_set=oracle_set,
                preauthored_generated=preauthored_generated,
            )
            _write_json(round_dir / "generated-manifest.json", manifest.model_dump(mode="json"))
            validate_generated_manifest(round_dir, manifest)

            mat = materialize_round(
                project_root=project_root,
                round_dir=round_dir,
                manifest=manifest,
                temp_factory=temp_factory,
            )
            last_workspace = mat.workspace
            workspace_destroyed = False
            try:
                observations = list(
                    runner.run_selection(
                        workspace=mat.workspace,
                        manifest=manifest,
                        seed=seed,
                        oracle_set_digest=oracle_set_digest,
                    )
                )
                sample_count += len(observations)
                for obs in observations:
                    covered_obligations.update(obs.obligation_ids)

                evals = evaluate_frozen_oracles(
                    oracle_set,
                    tuple((obs.oracle_id, obs.observation) for obs in observations),
                )
                for exec_obs, frozen in zip(observations, evals, strict=True):
                    ce = _maybe_confirm_counterexample(
                        exec_obs=exec_obs,
                        frozen_verdict=frozen.verdict.kind,
                        campaign_spec=campaign_spec,
                        round_id=round_id,
                        technique=technique,
                        seed=seed,
                        environment_digest=mat.receipt.environment_digest,
                        generated_file_digests={entry.source: entry.sha256 for entry in manifest.files},
                        oracle_set=oracle_set,
                        oracle_set_digest=oracle_set_digest,
                        runner=runner,
                        replay_attempts=replay_attempts,
                        change_dir=change_dir,
                        base_revision=base_revision,
                        recorded_at=observed_at,
                    )
                    if ce is None:
                        continue
                    all_ce_ids.append(ce.counterexample_id)
                    _write_json(
                        discovery_dir / "counterexamples" / f"{ce.counterexample_id}.json",
                        ce.model_dump(mode="json"),
                    )
                    if ce.finding_status == "confirmed":
                        confirmed_ids.append(ce.counterexample_id)
            finally:
                destroy_workspace(mat.workspace)
                workspace_destroyed = True
                last_workspace = None

            rounds_completed += 1
    finally:
        if last_workspace is not None and last_workspace.exists():
            destroy_workspace(last_workspace)
            workspace_destroyed = True

    missing = tuple(
        obl for obl in strategy_snapshot.required_obligation_ids if obl not in covered_obligations
    )
    status, stop_reason = _finalize_status(
        rounds_completed=rounds_completed,
        max_rounds=max_rounds,
        missing_obligations=missing,
        confirmed_count=len(confirmed_ids),
    )

    result = CampaignResult(
        schema_version="1",
        campaign_id=campaign_spec.campaign_id,
        change_id=campaign_spec.change_id,
        status=status,
        surfaces=campaign_spec.surfaces,
        rounds_completed=rounds_completed,
        sample_count=sample_count,
        seed=seed,
        counterexample_count=len(all_ce_ids),
        confirmed_count=len(confirmed_ids),
        stop_reason=stop_reason,
    )
    _write_json(discovery_dir / "campaign-result.json", result.model_dump(mode="json"))
    result_digest = sha256_bytes(
        json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    )

    ingest_result: CeIngestResult | None = None
    if ingest and confirmed_ids:
        issues_root = Path(issues_project_root) if issues_project_root is not None else project_root
        ingest_result = ingest_confirmed_counterexamples(
            change_dir,
            change_id=campaign_spec.change_id,
            batch_id=batch_id or f"DISC-{campaign_spec.campaign_id}",
            project_root=issues_root,
            reconcile=True,
            observed_at=observed_at,
        )
    elif ingest and not confirmed_ids:
        # Still run ingest so empty confirmed set is recorded consistently.
        issues_root = Path(issues_project_root) if issues_project_root is not None else project_root
        ingest_result = ingest_confirmed_counterexamples(
            change_dir,
            change_id=campaign_spec.change_id,
            batch_id=batch_id or f"DISC-{campaign_spec.campaign_id}",
            project_root=issues_root,
            reconcile=True,
            observed_at=observed_at,
        )

    return CampaignOutcome(
        result=result,
        result_digest=result_digest,
        counterexample_ids=tuple(confirmed_ids) if confirmed_ids else tuple(all_ce_ids),
        ingest=ingest_result,
        round_ids=tuple(round_ids),
        workspace_destroyed=workspace_destroyed,
    )


def _max_rounds(spec: CampaignSpec) -> int:
    if spec.budget is not None and spec.budget.max_rounds is not None:
        return spec.budget.max_rounds
    return 1


def _finalize_status(
    *,
    rounds_completed: int,
    max_rounds: int,
    missing_obligations: tuple[str, ...],
    confirmed_count: int,
) -> tuple[CampaignStatus, str | None]:
    """Choose CampaignResult status.

    Missing required obligation evidence must not look like an ordinary pass:
    prefer ``failed`` with structured reason (or ``stopped_budget`` when the
    only remaining signal is budget exhaustion without coverage).
    """
    _ = confirmed_count  # reserved for future fail-fast / finding stop reasons
    if missing_obligations:
        reason = "missing_required_obligation_evidence:" + ",".join(missing_obligations)
        if rounds_completed >= max_rounds:
            return "stopped_budget", reason
        return "failed", reason
    return "completed", None


def _prepare_generated_tree(
    *,
    round_dir: Path,
    campaign_spec: CampaignSpec,
    round_id: str,
    strategy_ids: tuple[str, ...],
    seed: int,
    base_revision: str,
    oracle_set: OracleSetSnapshot,
    preauthored_generated: Mapping[str, bytes] | None,
) -> GeneratedManifest:
    generated_root = round_dir / "generated"
    if generated_root.exists():
        # Round trees are write-once; refuse mutating an existing generated tree.
        raise CampaignError(
            "round_already_materialized",
            f"generated/ already exists under {round_id}",
        )

    files: list[GeneratedFileEntry] = []
    if preauthored_generated:
        for rel, data in sorted(preauthored_generated.items()):
            if ".." in rel or rel.startswith("/") or not rel.startswith("generated/"):
                raise CampaignError("unsafe_generated_path", f"unsafe preauthored path: {rel}")
            dest = round_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            # Map generated/tests/api/X -> tests/api/X
            if not rel.startswith("generated/tests/api/"):
                raise CampaignError(
                    "phase1_api_only",
                    f"Phase 1 preauthored files must live under generated/tests/api/: {rel}",
                )
            target = "tests/api/" + rel.removeprefix("generated/tests/api/")
            files.append(
                GeneratedFileEntry(
                    source=rel,
                    target=target,
                    sha256=digest_bytes(data),
                    role="search_test",
                )
            )
    else:
        body = _stub_bytes(seed=seed, campaign_id=campaign_spec.campaign_id, round_id=round_id)
        dest = round_dir / _STUB_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        files.append(
            GeneratedFileEntry(
                source=_STUB_REL,
                target=_STUB_TARGET,
                sha256=digest_bytes(body),
                role="search_test",
            )
        )

    if not files:
        raise CampaignError("empty_generated", "no generated files for round")

    oracle_refs = tuple(o.oracle_id for o in oracle_set.oracles)
    return GeneratedManifest(
        schema_version="1",
        change_id=campaign_spec.change_id,
        campaign_id=campaign_spec.campaign_id,
        round_id=round_id,
        parent_round_ids=(),
        base_revision=base_revision,
        strategy_ids=strategy_ids,
        files=tuple(files),
        execution_selection=tuple(f.target for f in files),
        oracle_refs=oracle_refs,
        seed=seed,
    )


def _stub_bytes(*, seed: int, campaign_id: str, round_id: str) -> bytes:
    # Deterministic minimal search stub — content is digest-pinned via manifest.
    text = (
        f"# aa-discovery-stub campaign={campaign_id} round={round_id} seed={seed}\n"
        f"def test_discovery_stub():\n"
        f"    assert True\n"
    )
    return text.encode("utf-8")


def _counterexample_id(
    *,
    campaign_id: str,
    round_id: str,
    oracle_id: str,
    seed: int,
    obligation_ids: tuple[str, ...],
) -> str:
    digest = sha256_bytes(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "obligation_ids": list(obligation_ids),
                "oracle_id": oracle_id,
                "round_id": round_id,
                "seed": seed,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    # sha256:<hex> → CE-<16 hex chars>
    hexpart = digest.removeprefix("sha256:")[:16]
    return f"CE-{hexpart}"


@dataclass
class _CapturingAttemptRunner:
    """Wrap a campaign runner to capture AttemptResults during replay."""

    inner: CampaignAttemptRunner
    captured: list[AttemptResult] = field(default_factory=list)

    def run_attempt(self, spec: ReplayAttemptSpec) -> AttemptResult:
        result = self.inner.run_attempt(spec)
        self.captured.append(result)
        return result


def _maybe_confirm_counterexample(
    *,
    exec_obs: ExecutionObservation,
    frozen_verdict: str,
    campaign_spec: CampaignSpec,
    round_id: str,
    technique: str,
    seed: int,
    environment_digest: str,
    generated_file_digests: dict[str, str],
    oracle_set: OracleSetSnapshot,
    oracle_set_digest: str,
    runner: CampaignAttemptRunner,
    replay_attempts: int,
    change_dir: Path,
    base_revision: str,
    recorded_at: str | None = None,
) -> Counterexample | None:
    """Build → replay → confirm for hard violate; heuristics never confirm."""
    if frozen_verdict in {"hold", "heuristic_signal", "inconclusive"}:
        # Search heuristics must not create confirmed CE / Problem.
        return None
    if frozen_verdict != "violate":
        return None

    oracle = next(o for o in oracle_set.oracles if o.oracle_id == exec_obs.oracle_id)
    if oracle.kind != "hard_oracle":
        # Defensive: evaluate_oracle remaps heuristic violate → heuristic_signal.
        return None

    ce_id = _counterexample_id(
        campaign_id=campaign_spec.campaign_id,
        round_id=round_id,
        oracle_id=exec_obs.oracle_id,
        seed=seed,
        obligation_ids=tuple(exec_obs.obligation_ids),
    )
    observed: dict[str, Any] = {"status_code": exec_obs.observation.status_code}
    if exec_obs.observation.claims:
        observed["claims"] = dict(exec_obs.observation.claims)

    candidate = Counterexample(
        schema_version="1",
        counterexample_id=ce_id,
        campaign_id=campaign_spec.campaign_id,
        round_id=round_id,
        surface="api",
        technique=technique,
        obligation_ids=tuple(exec_obs.obligation_ids),
        oracle_id=exec_obs.oracle_id,
        oracle_kind=oracle.kind,
        environment_digest=environment_digest,
        generated_file_digests=generated_file_digests,
        setup=dict(exec_obs.setup),
        actions=tuple(dict(a) for a in exec_obs.actions),
        observed=observed,
        expected={"verdict": "hold"},
        seed=seed,
        minimization=MinimizationInfo(status="raw", parent_counterexample_id=None),
        replay=CounterexampleReplay(attempts=replay_attempts, reproduced=0),
        finding_status="needs_review",
    )

    capturing = _CapturingAttemptRunner(inner=runner)
    aggregate = replay_counterexample(
        candidate,
        oracle,
        capturing,
        oracle_set_digest=oracle_set_digest,
        attempts=replay_attempts,
    )
    receipts = build_replay_attempt_receipts(
        counterexample_id=ce_id,
        seed=seed,
        base_revision=base_revision,
        oracle_set_digest=oracle_set_digest,
        oracle=oracle,
        attempt_results=aggregate.attempt_results or tuple(capturing.captured),
        recorded_at=recorded_at,
    )
    write_replay_attempt_receipts(change_dir, receipts)
    return confirm_counterexample(candidate, oracle_set, capturing.captured)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


__all__ = [
    "CampaignAttemptRunner",
    "CampaignError",
    "CampaignOutcome",
    "ExecutionObservation",
    "StrategySnapshot",
    "run_deterministic_api_campaign",
]
