import pytest

from assurance_agent.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    BoolOp,
    Call,
    Compare,
    DslError,
    Ident,
    Literal,
    Member,
    Subscript,
    collect_gate_refs,
    parse_expression,
)


def test_keyword_literals_map():
    assert parse_expression("true") == Literal(True)
    assert parse_expression("false") == Literal(False)
    assert parse_expression("null") == Literal(None)


def test_member_and_compare():
    node = parse_expression("params.run_mode == 'full'")
    assert isinstance(node, Compare)
    assert node.op == "=="
    assert isinstance(node.left, Member)
    assert node.left.prop == "run_mode"
    assert isinstance(node.left.obj, Ident)
    assert node.right == Literal("full")


def test_in_and_boolop():
    node = parse_expression("'api' in params.test_types and params.run_tests == true")
    assert isinstance(node, BoolOp)
    assert node.op == "and"


def test_two_arg_any_keeps_predicate_ast():
    node = parse_expression("any(fix_proposal.proposals, target == 'api' and eligible == true)")
    assert isinstance(node, Call)
    assert node.callee == "any"
    assert len(node.args) == 2
    assert isinstance(node.args[1], BoolOp)  # 谓词保留为 AST


def test_gate_ref_collection():
    node = parse_expression("gate('healing-entry-gate').verdict == 'enter'")
    assert collect_gate_refs(node) == ["healing-entry-gate"]


def test_node_call_parses_as_call_below_two_members():
    node = parse_expression("node('x').gate.verdict")
    assert isinstance(node, Member)
    assert node.prop == "verdict"
    assert isinstance(node.obj, Member)
    assert node.obj.prop == "gate"
    assert isinstance(node.obj.obj, Call)
    assert node.obj.obj.callee == "node"
    assert node.obj.obj.args == (Literal("x"),)
    assert BUILTIN_ARITY["node"] == 1


def test_subscript_constant_index():
    node = parse_expression("state.phases['skill-registry-check'].status == 'pass'")
    assert isinstance(node, Compare)
    assert isinstance(node.left, Member)  # .status
    assert isinstance(node.left.obj, Subscript)  # ['skill-registry-check']
    assert node.left.obj.index == "skill-registry-check"


def test_rejects_non_constant_subscript():
    with pytest.raises(DslError):
        parse_expression("state.phases[params.x]")  # 非常量下标不允许


def test_rejects_disallowed_nodes():
    with pytest.raises(DslError):
        parse_expression("__import__('os')")  # Call 到非白名单函数
    with pytest.raises(DslError):
        parse_expression("a + b")  # 二元算术不在白名单
    with pytest.raises(DslError):
        parse_expression("lambda x: x")  # lambda 不允许


def test_rejects_bare_equals():
    with pytest.raises(DslError):
        parse_expression("a = b")  # 语法错误（Python 赋值非表达式）


def test_plan_assurance_state_parses_with_four_args() -> None:
    node = parse_expression("plan_assurance_state(api_plan_checks, api_plan_review, data_knowledge, 'api')")
    assert isinstance(node, Call)
    assert node.callee == "plan_assurance_state"
    assert len(node.args) == 4
    assert BUILTIN_ARITY["plan_assurance_state"] == 4


def test_plan_assurance_state_rejects_wrong_arity() -> None:
    with pytest.raises(DslError, match="expects 4 arg"):
        parse_expression("plan_assurance_state(api_plan_checks, api_plan_review, data_knowledge)")
