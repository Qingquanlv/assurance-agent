from __future__ import annotations

from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1

from tests.product.test_product_stategraph_flow import (
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    result = invoke_product_root(_product_graphs(), "execute", _public_input("execute"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert tail.inspection is not None
    assert tail.report is not None


def test_coverage_insufficient_returns_without_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="coverage_insufficient"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "coverage_insufficient"
    assert tail.report is None
    assert tail.report_refs == ()


def test_needs_human_returns_without_test_repair_or_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="needs_human"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert tail.report is None
    assert "repair_result" not in result


def test_invalid_execution_result_blocks_before_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(execute={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "blocked"
    assert "inspection_outcome" not in result
    assert result.get("execution_result") in (None, {})
