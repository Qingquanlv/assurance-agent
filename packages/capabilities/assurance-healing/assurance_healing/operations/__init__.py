from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_healing.agent_ops.apply_test_repair import (
    finalize as apply_test_repair_finalize,
    prepare as apply_test_repair_prepare,
)
from assurance_healing.agent_ops.coverage_repair import (
    finalize as coverage_repair_finalize,
    prepare as coverage_repair_prepare,
)
from assurance_healing.agent_ops.fix_proposal import (
    finalize as fix_proposal_finalize,
    prepare as fix_proposal_prepare,
)

__all__ = [
    "handlers",
    "healing_handlers",
]


def healing_handlers() -> dict[str, TaskHandler]:
    return dict(handlers())


def handlers() -> Mapping[str, TaskHandler]:
    from assurance_healing.operations.proposal import healing_handlers

    registered = healing_handlers()
    registered.update(
        {
            "assurance.healing.apply-test-repair.finalize": cast(TaskHandler, apply_test_repair_finalize),
            "assurance.healing.apply-test-repair.prepare": cast(TaskHandler, apply_test_repair_prepare),
            "assurance.healing.coverage-repair.finalize": cast(TaskHandler, coverage_repair_finalize),
            "assurance.healing.coverage-repair.prepare": cast(TaskHandler, coverage_repair_prepare),
            "assurance.healing.fix-proposal.finalize": cast(TaskHandler, fix_proposal_finalize),
            "assurance.healing.fix-proposal.prepare": cast(TaskHandler, fix_proposal_prepare),
        }
    )
    return MappingProxyType(registered)
