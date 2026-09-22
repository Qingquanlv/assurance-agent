"""Host-side collector normalization and family runner argv."""

from __future__ import annotations

from assurance_execution.contracts.execution import ExecutionFamily
from assurance_execution.contracts.observations import CollectorDocumentV1, PytestReportV1
from assurance_execution.operations.common import OutputError


OBSERVE_CONTEXT_FLAG = "--assurance-observe-context="
OBSERVE_OUTPUT_FLAG = "--assurance-observe-output="


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
    observe_context: str | None = None,
    observe_output: str | None = None,
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
    # Host-only flags. The process host strips them from the public argv and
    # turns them into the collector's environment.
    argv.append(f"--assurance-batch-id={batch_id}")
    if observe_context is not None:
        argv.append(f"{OBSERVE_CONTEXT_FLAG}{observe_context}")
    if observe_output is not None:
        argv.append(f"{OBSERVE_OUTPUT_FLAG}{observe_output}")
    return tuple(argv)
