"""Driver-owned full-scope test-infrastructure bootstrap gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import Ledger
from assurance_agent.workflow.core.state import read_state, write_state

TEST_INFRA_FILES = (
    "tests/config.py",
    "tests/conftest.py",
    "tests/schema_validation.py",
)


@dataclass(frozen=True)
class BootstrapCheckResult:
    kind: Literal["ready", "needs_human"]
    reason: str | None = None
    present: tuple[str, ...] = ()
    skipped: bool = False


def evaluate_test_infra_bootstrap(project_root: Path, change_dir: Path) -> BootstrapCheckResult:
    decision = Ledger(change_dir).latest(type="human_decision", checkpoint="bootstrap")
    if decision is not None and decision.get("action") == "skip_branch":
        return BootstrapCheckResult(kind="ready", skipped=True)

    present = tuple(rel for rel in TEST_INFRA_FILES if (project_root / rel).is_file())
    missing = tuple(rel for rel in TEST_INFRA_FILES if rel not in present)
    if not missing:
        return BootstrapCheckResult(kind="ready", present=present)
    return BootstrapCheckResult(
        kind="needs_human",
        reason=(
            f"Test infra not ready (missing {', '.join(missing)}). "
            "Scaffold the test infrastructure or record: "
            "aa decide --at bootstrap --action skip_branch --reason <decision>"
        ),
        present=present,
    )


def mark_test_infra_bootstrap_done(change_dir: Path, result: BootstrapCheckResult) -> None:
    state = read_state(change_dir)
    data = state.model_dump(mode="python", exclude_none=True)
    phases = data.setdefault("phases", {})
    assert isinstance(phases, dict)
    phases["test_infra_bootstrap"] = {
        "status": "done",
        "skill_loaded": True,
        "skill": "aa-test-infra-bootstrap",
        "files": list(TEST_INFRA_FILES),
        "kept": list(result.present),
        "skipped": result.skipped,
    }
    write_state(change_dir, WorkflowState.model_validate(data))
