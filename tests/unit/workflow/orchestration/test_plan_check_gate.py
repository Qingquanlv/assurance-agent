"""check 报事实、policy 定处置：同一份 evidence 在不同 policy 下走不同 verdict。"""

from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression

REJECT_WHEN = (
    "api_plan_review.decision == 'reject' "
    "or api_plan_review.codegen_readiness == 'not_ready' "
    "or (defined(api_plan_checks.status) "
    "and api_plan_checks.status == 'fail' and policy.plan_check_action == 'block')"
)
HUMAN_WHEN = (
    "defined(api_plan_checks.status) "
    "and api_plan_checks.status == 'fail' and policy.plan_check_action == 'require_human'"
)


def _scope(check_status: str, action: str) -> Scope:
    return Scope(
        {
            "api_plan_review": {"decision": "pass", "codegen_readiness": "ready"},
            "api_plan_checks": {"status": check_status},
            "policy": {"plan_check_action": action},
        }
    )


def _scope_without_checks(action: str) -> Scope:
    """Checks 产物缺失（recover 路径、历史 change 目录）时的 fail-open 语义。"""
    return Scope(
        {
            "api_plan_review": {"decision": "pass", "codegen_readiness": "ready"},
            "api_plan_checks": None,
            "policy": {"plan_check_action": action},
        }
    )


def test_warn_policy_lets_a_failing_check_through() -> None:
    assert evaluate(parse_expression(REJECT_WHEN), _scope("fail", "warn")) is False
    assert evaluate(parse_expression(HUMAN_WHEN), _scope("fail", "warn")) is False


def test_block_policy_rejects_a_failing_check() -> None:
    assert evaluate(parse_expression(REJECT_WHEN), _scope("fail", "block")) is True


def test_require_human_policy_routes_to_human_review() -> None:
    assert evaluate(parse_expression(HUMAN_WHEN), _scope("fail", "require_human")) is True
    assert evaluate(parse_expression(REJECT_WHEN), _scope("fail", "require_human")) is False


def test_passing_check_is_inert_under_every_policy() -> None:
    for action in ("warn", "block", "require_human"):
        assert evaluate(parse_expression(REJECT_WHEN), _scope("pass", action)) is False
        assert evaluate(parse_expression(HUMAN_WHEN), _scope("pass", action)) is False


def test_absent_check_artifact_yields_false_not_missing() -> None:
    """defined() 守卫把缺失 evidence 压成 False，而不是 MISSING。"""
    for action in ("warn", "block", "require_human"):
        assert evaluate(parse_expression(REJECT_WHEN), _scope_without_checks(action)) is False
        assert evaluate(parse_expression(HUMAN_WHEN), _scope_without_checks(action)) is False


def test_schema_gate_wires_the_check_artifact() -> None:
    from assurance_agent import resources

    text = resources.read_text("schemas", "workflow-schema.yaml")
    assert "review/api-plan-checks.json, as: api_plan_checks" in text
    assert "policy.plan_check_action == 'block'" in text


def test_every_policy_field_has_a_runtime_consumer() -> None:
    """Each policy field has a gate expression that consumes it."""
    from assurance_agent import resources
    from assurance_agent.artifacts.models.policy import Policy

    schema = resources.read_text("schemas", "workflow-schema.yaml")
    unconsumed = {
        name for name in Policy.model_fields if name != "version" and f"policy.{name}" not in schema
    }
    assert unconsumed == set()
