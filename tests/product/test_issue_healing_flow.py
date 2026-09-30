from __future__ import annotations

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.routes import route_applied_repair
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1

from tests.product.test_product_stategraph_flow import (
    _applied,
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)


def test_applied_test_repair_is_the_only_path_to_rerun() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=(_inspection(disposition="repairable_execution_failure"), _inspection()),
                repair_failure=_applied(),
            )
        ),
        "full",
        _public_input("full"),
    )
    execution = ExecutionCycleResultV1.model_validate(result["execution_result"])
    assert execution.repair_round == 1
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"


def test_fix_proposal_output_cannot_parse_as_applied_repair() -> None:
    proposal_only = {
        "proposal_result": {"schema_version": "1", "change_id": "CH-DEMO-001"},
        "status": "passed",
    }
    assert route_applied_repair(proposal_only) == "blocked"
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition="repairable_execution_failure"),
                repair_failure=proposal_only,
            )
        ),
        "full",
        _public_input("full"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert ExecutionCycleResultV1.model_validate(result["execution_result"]).repair_round == 0


def test_non_applied_repair_status_is_blocked() -> None:
    repair = {
        "repair_result": {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": 0,
            "repair_round": 1,
            "status": "exhausted",
        }
    }
    assert route_applied_repair(repair) == "blocked"
