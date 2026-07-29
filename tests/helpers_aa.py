"""Shared project fixtures for tests that resolve Change paths via config."""

from datetime import UTC, datetime
from pathlib import Path

from assurance_agent.change_location import ChangeLocation, ChangeSource
from assurance_agent.evidence.sufficiency import EvidenceCoverageEvaluation, SufficiencyReport
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def sufficient_evidence_coverage(
    *,
    action: str = "warn",
) -> EvidenceCoverageEvaluation:
    """Stub evidence evaluation for tests that seed execution artifacts only."""
    return EvidenceCoverageEvaluation(
        report=SufficiencyReport(
            as_of=datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC),
            recency_hours=72,
            verdicts=(),
        ),
        action=action,  # type: ignore[arg-type]
        error_code=None,
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
