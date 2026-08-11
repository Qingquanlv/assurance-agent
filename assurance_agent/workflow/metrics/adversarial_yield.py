"""``operation:collect-adversarial-yield`` — nightly B3 discovery yield (§5-B3).

Reads Change-local discovery receipts only (``discovery/counterexamples/*.yaml``
and optional ``discovery/campaign-result.yaml``). Never invents yield zeros for
mere absence: empty discovery → ``not_evaluated`` + ``pending_nightly``. Writes
batch evidence at ``execution/runs/nightly/adversarial-yield.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.discovery import CampaignResult, Counterexample
from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot
from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import AdversarialYieldEvidence
from assurance_agent.evidence.replay_telemetry import compute_seed_replay_rate
from assurance_agent.workflow.discovery.replay_receipts import (
    ReplayReceiptIntegrityError,
    load_replay_attempt_receipts,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace

ADVERSARIAL_YIELD_BATCH_ID = "nightly"
ADVERSARIAL_YIELD_EVIDENCE_REL = f"execution/runs/{ADVERSARIAL_YIELD_BATCH_ID}/adversarial-yield.json"

_CE_DIR_REL = "discovery/counterexamples"
_CAMPAIGN_RESULT_REL = "discovery/campaign-result.yaml"
_ORACLE_SET_REL = "discovery/oracle-set.yaml"
_ISSUE_SNAPSHOT_REL = "issues/snapshot.json"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pending(
    *, change_id: str, batch_id: str, source: dict[str, str] | None = None
) -> AdversarialYieldEvidence:
    return AdversarialYieldEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        property="api",
        layer="api",
        sample_count=0,
        counterexample_ids=(),
        unclosed_count=0,
        seed=0,
        status="not_evaluated",
        value=None,
        shortboards=(MetricShortboard(code="pending_nightly", metric="adversarial_yield"),),
        source=source or {},
    )


def _failed(
    *,
    change_id: str,
    batch_id: str,
    gap: MetricCollectionGap,
    source: dict[str, str] | None = None,
) -> AdversarialYieldEvidence:
    return AdversarialYieldEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        property="api",
        layer="api",
        sample_count=0,
        counterexample_ids=(),
        unclosed_count=0,
        seed=0,
        status="collection_failed",
        value=None,
        collection_gaps=(gap,),
        source=source or {},
    )


def _ce_paths(change_dir: Path) -> list[Path]:
    ce_dir = change_dir / _CE_DIR_REL
    if not ce_dir.is_dir():
        return []
    paths = sorted(ce_dir.glob("*.yaml")) + sorted(ce_dir.glob("*.yml"))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _load_raw_mapping(path: Path) -> dict[str, Any] | MetricCollectionGap:
    rel = f"{_CE_DIR_REL}/{path.name}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        return MetricCollectionGap(
            code="artifact_corrupt",
            metric="adversarial_yield",
            detail=f"{rel} unreadable: {err}",
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        return MetricCollectionGap(
            code="artifact_corrupt",
            metric="adversarial_yield",
            detail=f"{rel} unparseable: {err}",
        )
    if not isinstance(data, dict):
        return MetricCollectionGap(
            code="artifact_corrupt",
            metric="adversarial_yield",
            detail=f"{rel} must be a mapping",
        )
    return data


def _closed_counterexample_ids(
    change_dir: Path,
    *,
    expected_change_id: str,
) -> frozenset[str] | MetricCollectionGap:
    """Join immutable CE evidence refs through Observation/Occurrence to Problem."""
    path = change_dir / _ISSUE_SNAPSHOT_REL
    if not path.is_file():
        return frozenset()
    try:
        snapshot = ChangeIssueSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
        return MetricCollectionGap(
            code="artifact_corrupt",
            metric="adversarial_yield",
            detail=f"{_ISSUE_SNAPSHOT_REL} unparseable: {err}",
        )
    if snapshot.change_id != expected_change_id:
        return MetricCollectionGap(
            code="identity_mismatch",
            metric="adversarial_yield",
            detail=(f"{_ISSUE_SNAPSHOT_REL} change_id={snapshot.change_id!r} != {expected_change_id!r}"),
        )

    observations = {item.observation_id: item for item in snapshot.observations}
    closed: set[str] = set()
    for occurrence in snapshot.occurrences:
        if not occurrence.problem_id.strip():
            continue
        for observation_id in occurrence.observation_ids:
            observation = observations.get(observation_id)
            if observation is None:
                continue
            artifact = observation.source.artifact
            prefix = f"{_CE_DIR_REL}/"
            if not artifact.startswith(prefix):
                continue
            filename = artifact.removeprefix(prefix)
            if "/" in filename or not filename.endswith((".yaml", ".yml")):
                continue
            closed.add(filename.rsplit(".", 1)[0])
    return frozenset(closed)


def collect_adversarial_yield(
    *,
    change_dir: Path,
    change_id: str,
    batch_id: str = ADVERSARIAL_YIELD_BATCH_ID,
) -> AdversarialYieldEvidence:
    """Collect B3 yield from discovery CE / campaign receipts (discovery-only I/O)."""
    source: dict[str, str] = {
        "counterexamples_rel": _CE_DIR_REL,
        "campaign_result_rel": _CAMPAIGN_RESULT_REL,
    }
    ce_paths = _ce_paths(change_dir)
    campaign_path = change_dir / _CAMPAIGN_RESULT_REL
    campaign: CampaignResult | None = None

    if campaign_path.is_file():
        try:
            raw_campaign = yaml.safe_load(campaign_path.read_text(encoding="utf-8"))
            campaign = CampaignResult.model_validate(raw_campaign)
            source["campaign_result_sha256"] = _sha256_file(campaign_path)
        except (OSError, ValueError, yaml.YAMLError, ValidationError) as err:
            return _failed(
                change_id=change_id,
                batch_id=batch_id,
                gap=MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="adversarial_yield",
                    detail=f"{_CAMPAIGN_RESULT_REL} unparseable: {err}",
                ),
                source=source,
            )
        if campaign.change_id != change_id:
            return _failed(
                change_id=change_id,
                batch_id=batch_id,
                gap=MetricCollectionGap(
                    code="identity_mismatch",
                    metric="adversarial_yield",
                    detail=(f"campaign-result change_id={campaign.change_id!r} != {change_id!r}"),
                ),
                source=source,
            )
        if campaign.sample_count is None or campaign.seed is None:
            return _failed(
                change_id=change_id,
                batch_id=batch_id,
                gap=MetricCollectionGap(
                    code="identity_mismatch",
                    metric="adversarial_yield",
                    detail="campaign-result requires sample_count and seed",
                ),
                source=source,
            )

    oracle_path = change_dir / _ORACLE_SET_REL
    if oracle_path.is_file():
        try:
            source["oracle_set_sha256"] = _sha256_file(oracle_path)
        except OSError:
            pass

    if not ce_paths and campaign is None:
        return _pending(change_id=change_id, batch_id=batch_id, source=source)

    confirmed: list[Counterexample] = []
    current_counterexample_ids: set[str] = set()
    all_readable = 0
    seeds: list[int] = []
    digest_parts: list[str] = []

    for path in ce_paths:
        rel = f"{_CE_DIR_REL}/{path.name}"
        raw = _load_raw_mapping(path)
        if isinstance(raw, MetricCollectionGap):
            return _failed(change_id=change_id, batch_id=batch_id, gap=raw, source=source)

        # A change directory may retain receipts from older campaigns. They are
        # valid history, but cannot contribute to the current campaign metric.
        if campaign is not None and raw.get("campaign_id") != campaign.campaign_id:
            continue

        if "seed" not in raw or raw["seed"] is None:
            return _failed(
                change_id=change_id,
                batch_id=batch_id,
                gap=MetricCollectionGap(
                    code="identity_mismatch",
                    metric="adversarial_yield",
                    detail=f"seed missing on {rel}",
                ),
                source=source,
            )

        try:
            ce = Counterexample.model_validate(raw)
        except ValidationError as err:
            return _failed(
                change_id=change_id,
                batch_id=batch_id,
                gap=MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="adversarial_yield",
                    detail=f"{rel} invalid: {err}",
                ),
                source=source,
            )

        all_readable += 1
        current_counterexample_ids.add(ce.counterexample_id)
        seeds.append(ce.seed)
        digest_parts.append(f"{ce.counterexample_id}:{_sha256_file(path)}")
        if ce.finding_status == "confirmed":
            confirmed.append(ce)

    if digest_parts:
        source["counterexamples_sha256"] = hashlib.sha256("\n".join(digest_parts).encode("utf-8")).hexdigest()

    if campaign is not None and campaign.counterexample_count == 0 and all_readable == 0:
        # Honest zero from a completed empty campaign — still evaluated.
        return AdversarialYieldEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            property="api",
            layer="api",
            sample_count=campaign.sample_count or 0,
            counterexample_ids=(),
            unclosed_count=0,
            seed=campaign.seed or 0,
            status="evaluated",
            value=0.0,
            source=source,
        )

    if campaign is not None and all_readable != campaign.counterexample_count:
        # Campaign present but CE files absent — typed gap, not silent zero.
        return _failed(
            change_id=change_id,
            batch_id=batch_id,
            gap=MetricCollectionGap(
                code="collection_failed",
                metric="adversarial_yield",
                detail=(
                    f"campaign-result counterexample_count={campaign.counterexample_count} "
                    f"but current campaign has {all_readable} readable receipts"
                ),
            ),
            source=source,
        )

    if campaign is None:
        return _failed(
            change_id=change_id,
            batch_id=batch_id,
            gap=MetricCollectionGap(
                code="identity_mismatch",
                metric="adversarial_yield",
                detail="campaign-result is required to establish sample_count and seed",
            ),
            source=source,
        )
    assert campaign.sample_count is not None
    assert campaign.seed is not None

    unique_seeds = sorted(set(seeds))
    if not unique_seeds:
        return _failed(
            change_id=change_id,
            batch_id=batch_id,
            gap=MetricCollectionGap(
                code="identity_mismatch",
                metric="adversarial_yield",
                detail="seed missing on campaign / counterexample receipts",
            ),
            source=source,
        )
    if len(unique_seeds) != 1 or unique_seeds[0] != campaign.seed:
        return _failed(
            change_id=change_id,
            batch_id=batch_id,
            gap=MetricCollectionGap(
                code="identity_mismatch",
                metric="adversarial_yield",
                detail="counterexample seed does not match campaign-result seed",
            ),
            source=source,
        )
    seed = campaign.seed

    closed_ids = _closed_counterexample_ids(
        change_dir,
        expected_change_id=change_id,
    )
    if isinstance(closed_ids, MetricCollectionGap):
        return _failed(change_id=change_id, batch_id=batch_id, gap=closed_ids, source=source)

    confirmed_ids = tuple(sorted({ce.counterexample_id for ce in confirmed}))
    unclosed = sum(1 for ce in confirmed if ce.counterexample_id not in closed_ids)
    sample_count = campaign.sample_count

    # C3 summary: only when immutable attempt receipts exist; else leave unset
    # (not_evaluated) — never invent 0/0 = 1.0.
    try:
        receipts = tuple(
            receipt
            for receipt in load_replay_attempt_receipts(change_dir)
            if receipt.counterexample_id in current_counterexample_ids
        )
    except ReplayReceiptIntegrityError as err:
        return _failed(
            change_id=change_id,
            batch_id=batch_id,
            gap=MetricCollectionGap(
                code="artifact_corrupt",
                metric="adversarial_yield",
                detail=str(err),
            ),
            source=source,
        )
    replay_success, replay_attempts, replay_rate = compute_seed_replay_rate(receipts)
    if replay_attempts == 0:
        replay_success_out: int | None = None
        replay_attempts_out: int | None = None
        replay_rate_out: float | None = None
    else:
        replay_success_out = replay_success
        replay_attempts_out = replay_attempts
        replay_rate_out = replay_rate
        source["replay_receipts_count"] = str(replay_attempts)

    return AdversarialYieldEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        property="api",
        layer="api",
        sample_count=sample_count,
        counterexample_ids=confirmed_ids,
        unclosed_count=unclosed,
        seed=seed,
        status="evaluated",
        value=float(len(confirmed_ids)),
        seed_replay_success=replay_success_out,
        seed_replay_attempts=replay_attempts_out,
        seed_replay_rate=replay_rate_out,
        source=source,
    )


def collect_adversarial_yield_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Side-effecting op: collect discovery yield and write nightly evidence once."""
    del task
    change_id = context.change_id or workspace.change_dir.name
    evidence = collect_adversarial_yield(
        change_dir=workspace.change_dir,
        change_id=change_id,
    )
    out = workspace.change_dir / ADVERSARIAL_YIELD_EVIDENCE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(evidence))
    return TaskResult(
        status="succeeded",
        value={
            "path": ADVERSARIAL_YIELD_EVIDENCE_REL,
            "status": evidence.status,
            "value": evidence.value,
            "unclosed_count": evidence.unclosed_count,
            "sample_count": evidence.sample_count,
        },
    )


__all__ = [
    "ADVERSARIAL_YIELD_BATCH_ID",
    "ADVERSARIAL_YIELD_EVIDENCE_REL",
    "collect_adversarial_yield",
    "collect_adversarial_yield_operation",
]
