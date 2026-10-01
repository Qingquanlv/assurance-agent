from __future__ import annotations

from pathlib import Path

from graph_engine.plugin_api import RegistryPorts
from graph_engine import ENGINE_API_VERSION

from assurance_intake import ops
from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.plugin import IntakePlugin

_PACKAGE = Path(ops.__file__).resolve().parent.parent


def test_every_op_directory_declares_exactly_one_op() -> None:
    directories = sorted(
        path.name for path in (_PACKAGE / "ops").iterdir() if (path / "__init__.py").is_file()
    )
    declared = ops.router.ops()
    assert sorted(op.directory for op in declared.values()) == directories
    assert tuple(declared) == (
        "case-design",
        "case-repair",
        "case-review",
        "explore",
        "intake",
        "resolve-plan",
    )
    assert tuple(AGENT_JOB_CONTRACTS) == ("case-design", "case-repair", "case-review", "explore", "intake")
    assert tuple(TASK_ATTEMPT_CONTRACTS) == ("resolve-plan",)


def test_every_op_resource_is_published() -> None:
    on_disk = {
        path.relative_to(_PACKAGE).as_posix()
        for path in (_PACKAGE / "ops").rglob("*")
        if path.suffix in {".md", ".json"}
    }
    assert set(IntakePlugin.spec.resource_files.values()) == on_disk


def test_contribution_handlers_and_attempt_contracts_come_from_the_router() -> None:
    contribution = IntakePlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert tuple(contribution.task_handlers) == tuple(sorted(ops.router.routes()))
    assert contribution.attempt_contracts == ops.router.attempt_contract_refs()
