import pytest

from assurance_agent.workflow.orchestration.dsl import (
    DslError,
    MISSING,
    Scope,
    evaluate,
    is_satisfied,
    parse_expression,
)


def ev(text, vars_, **kw):
    return evaluate(parse_expression(text), Scope(vars_, **kw))


def test_missing_propagation():
    assert ev("state.x == 'y'", {"state": {}}) is MISSING
    assert ev("not state.x", {"state": {}}) is MISSING
    assert ev("state.x == 'y' and false", {"state": {}}) is False  # F 短路
    assert ev("state.x == 'y' or true", {"state": {}}) is True  # T 短路
    assert ev("state.x == 'y' and true", {"state": {}}) is MISSING


def test_typed_equality():
    assert ev("a == 1", {"a": 1}) is True
    assert ev("a == '1'", {"a": 1}) is False  # 无跨类型相等
    assert ev("a in ['x', 'y']", {"a": "y"}) is True
    assert ev("a in b", {"a": 1, "b": "notalist"}) is MISSING


def test_in_uses_params_list():
    vars_ = {"params": {"test_types": ["api", "e2e"]}}
    assert ev("'api' in params.test_types", vars_) is True
    assert ev("'fuzz' in params.test_types", vars_) is False


def test_any_all_count_child_scope():
    vars_ = {
        "fix_proposal": {
            "proposals": [
                {"target": "api", "eligible": True},
                {"target": "e2e", "eligible": False},
            ]
        }
    }
    assert ev("any(fix_proposal.proposals, target == 'api' and eligible == true)", vars_) is True
    assert ev("all(fix_proposal.proposals, eligible == true)", vars_) is False
    assert ev("count(fix_proposal.proposals, eligible == true)", vars_) == 1


def test_any_empty_and_missing():
    assert ev("any(xs, eligible == true)", {"xs": []}) is False
    assert ev("all(xs, eligible == true)", {"xs": []}) is True
    assert ev("any(xs, eligible == true)", {"xs": [{}]}) is MISSING  # 元素无 eligible


def test_builtins():
    assert ev("len(a) > 0", {"a": [1, 2]}) is True
    assert ev("defined(a)", {"a": None}) is True  # None 是已定义
    assert ev("defined(a)", {}) is False
    assert ev("file_exists('healing/x.json')", {}, file_exists=lambda p: True) is True
    assert ev("gate('g').verdict == 'enter'", {}, gate_verdict=lambda i: "enter") is True


def test_node_result_builtin() -> None:
    assert (
        ev(
            "node('review').gate.verdict == 'pass'",
            {},
            node_result=lambda node_id: {"gate": {"verdict": "pass"}} if node_id == "review" else {},
        )
        is True
    )


def test_node_result_without_resolver_fails_closed() -> None:
    with pytest.raises(DslError, match="no resolver"):
        ev("node('review').value == true", {})


def test_subscript_eval():
    vars_ = {"state": {"phases": {"skill-registry-check": {"status": "pass"}}}}
    assert ev("state.phases['skill-registry-check'].status == 'pass'", vars_) is True
    # 缺键 → MISSING → fail-closed
    assert ev("state.phases['nope'].status == 'pass'", vars_) is MISSING
    # x['k'] 与 x.k 等价
    assert ev("state['phases']['skill-registry-check']['status'] == 'pass'", vars_) is True


def test_is_satisfied_fail_closed():
    assert is_satisfied(parse_expression("state.x == 'y'"), Scope({"state": {}})) is False
