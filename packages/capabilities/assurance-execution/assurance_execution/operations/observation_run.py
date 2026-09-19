"""Host-side collector normalization and family runner argv."""

from __future__ import annotations

from assurance_execution.contracts.execution import ExecutionFamily
from assurance_execution.contracts.observations import CollectorDocumentV1, PytestReportV1
from assurance_execution.operations.common import OutputError


class RunnerUnsupported(ValueError):
    """The selected family has no deterministic runner in this slice."""


def normalize_collector_report(document: CollectorDocumentV1) -> PytestReportV1:
    if not document.complete or document.collection_errors:
        raise OutputError("collector_incomplete")
    return document.report


def build_family_argv(
    family: ExecutionFamily,
    selected: tuple[str, ...],
    *,
    batch_id: str,
) -> tuple[str, ...]:
    if family == "performance" or family not in {"api", "e2e", "fuzz"}:
        raise RunnerUnsupported(f"runner_unsupported:{family}")
    argv = [
        "uv",
        "run",
        "--isolated",
        "--frozen",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "aa_observe",
        "--tb=line",
        "-o",
        "pythonpath=qa",
        *selected,
    ]
    if family == "e2e":
        argv.append(f"--output=/tmp/aa-playwright-{batch_id}")
    return tuple(argv)
