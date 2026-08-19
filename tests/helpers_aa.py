"""Shared project fixtures for tests that resolve Change paths via config."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assurance_agent.artifacts.models.sufficiency import SufficiencyReportV2
from assurance_agent.change_location import ChangeLocation, ChangeSource
from assurance_agent.evidence.sufficiency import EvidenceCoverageEvaluation
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml

AWARE_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def make_verdict(
    *,
    case_id: str = "TC_API_001",
    sufficient: bool | None = None,
    missing_kinds: list[str] | None = None,
    reason_codes: list[str] | None = None,
    execution_state: str | None = None,
) -> dict[str, Any]:
    """Build a raw SufficiencyRowVerdictV2 payload for model_validate tests."""
    kinds = list(missing_kinds or [])
    reasons = list(reason_codes or [])
    if sufficient is None:
        sufficient = not kinds and not reasons
    if execution_state is None:
        if ("execution_recent", "never_run") in zip(kinds, reasons, strict=False) or (
            len(kinds) == 1 and kinds[0] == "execution_recent" and reasons == ["never_run"]
        ):
            execution_state = "never_run"
        elif ("execution_recent", "execution_stale") in zip(kinds, reasons, strict=False) or (
            len(kinds) == 1 and kinds[0] == "execution_recent" and reasons == ["execution_stale"]
        ):
            execution_state = "stale"
        else:
            execution_state = "fresh"
    return {
        "case_id": case_id,
        "sufficient": sufficient,
        "missing_kinds": kinds,
        "reason_codes": reasons,
        "execution_state": execution_state,
    }


def make_report_v2(
    *,
    source_projection_digest: str = "a" * 64,
    source_policy_digest: str = "b" * 64,
    require_current_batch: bool = True,
    as_of: datetime = AWARE_NOW,
    recency_hours: int = 72,
    verdicts: list[dict[str, Any]] | None = None,
    semantics: str = "evidence_sufficiency/v2",
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "source_projection_digest": source_projection_digest,
        "source_policy_digest": source_policy_digest,
        "semantics": semantics,
        "require_current_batch": require_current_batch,
        "as_of": as_of.isoformat(),
        "recency_hours": recency_hours,
        "verdicts": verdicts if verdicts is not None else [make_verdict()],
    }


def make_layer_counts(
    *,
    layer: str = "api",
    case_type: str = "API",
    sufficient: int = 0,
    insufficient: int = 0,
    reason_counts: dict[str, int] | None = None,
    execution_state_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    states = execution_state_counts
    if states is None:
        total = sufficient + insufficient
        states = {"never_run": 0, "stale": 0, "fresh": total}
    return {
        "layer": layer,
        "case_type": case_type,
        "sufficient": sufficient,
        "insufficient": insufficient,
        "reason_counts": reason_counts if reason_counts is not None else {},
        "execution_state_counts": states,
    }


def sufficient_evidence_coverage(
    *,
    action: str = "warn",
) -> EvidenceCoverageEvaluation:
    """Stub evidence evaluation for tests that seed execution artifacts only."""
    return EvidenceCoverageEvaluation(
        report=SufficiencyReportV2.model_validate(make_report_v2(verdicts=[])),
        action=action,  # type: ignore[arg-type]
        error_code=None,
    )


def overlay_workflow_yaml(
    *,
    node: str,
    uses: str,
    name: str = "project-custom",
    entrypoint: str = "my-pipeline",
) -> str:
    """Minimal v2 workflow YAML: one operation node from START to END."""
    return textwrap.dedent(
        f"""\
        name: {name}
        entrypoints:
          {entrypoint}: {{graph: main, restart: repeatable}}
        policies:
          retry:
            never: {{max_attempts: 1, retry_on: []}}
          timeout:
            local: {{run_seconds: 60, heartbeat_seconds: 10}}
          scheduler: {{max_parallel_tasks: 1}}
        graphs:
          main:
            max_supersteps: 4
            nodes:
              {node}:
                uses: {uses}
                retry: never
                timeout: local
            edges:
              - {{from: START, to: {node}}}
              - {{from: {node}, to: END}}
        gates: {{}}
        """
    )


def write_aa_config(project_root: Path) -> None:
    """Write a valid `.aa/config.yaml` (required by resolve_change)."""
    aa = project_root / ".aa"
    aa.mkdir(parents=True, exist_ok=True)
    path = aa / "config.yaml"
    if not path.is_file():
        path.write_text(build_config_yaml(InitAnswers()), encoding="utf-8")


def loc_for(
    change_dir: Path,
    *,
    project_root: Path | None = None,
    source: ChangeSource = "changes",
) -> ChangeLocation:
    """Wrap a change directory as a ``ChangeLocation`` for unit tests.

    ``project_root`` defaults to ``change_dir`` (fine for change-relative reads);
    pass it explicitly when a schema uses ``qa/`` / ``repo:`` prefixed paths.
    """
    return ChangeLocation(
        project_root=project_root if project_root is not None else change_dir,
        change_id=change_dir.name,
        path=change_dir,
        source=source,
    )
