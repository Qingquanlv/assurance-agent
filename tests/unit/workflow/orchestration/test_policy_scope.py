"""policy 常量以命名绑定进入 gate 作用域，风险等级字面量从拓扑里消失（spec C3）。"""

from pathlib import Path

from assurance_agent.artifacts.policy import load_policy
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression


def _scope(project_root: Path) -> Scope:
    return Scope(
        {
            "api_plan_review": {"risk_level": "high", "human_review_required": False},
            "params": {"force_continue": False},
            "policy": load_policy(project_root).model_dump(mode="json"),
        }
    )


def test_in_operator_accepts_a_policy_list(tmp_path: Path) -> None:
    expr = parse_expression("api_plan_review.risk_level in policy.human_review_risk_levels")
    assert evaluate(expr, _scope(tmp_path)) is True


def test_policy_change_flips_the_verdict_without_touching_the_schema(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "policy.yaml").write_text(
        "version: 1\n"
        "human_review_risk_levels: [critical]\n"
        "force_continue_allowed: true\n"
        "plan_checks:\n"
        "  l1_path: warn\n"
        "  shared_factory: warn\n"
        "  assert_ideal: warn\n"
        "  capability_keys: warn\n"
        "coverage_floor: {risk_high: 0.9, risk_medium: 0.7}\n"
        "fuzz: {required_when_endpoint_has_auth: true}\n"
        "healing: {auth_module: require_human}\n",
        encoding="utf-8",
    )
    expr = parse_expression("api_plan_review.risk_level in policy.human_review_risk_levels")
    assert evaluate(expr, _scope(tmp_path)) is False


def test_scalar_policy_constant_is_comparable(tmp_path: Path) -> None:
    expr = parse_expression("policy.plan_checks.assert_ideal == 'warn'")
    assert evaluate(expr, _scope(tmp_path)) is True


BYPASS = (
    "params.force_continue == true "
    "and policy.force_continue_allowed == true "
    "and api_plan_review.decision == 'pass'"
)


def _bypass_scope(force_continue: bool, allowed: bool) -> Scope:
    return Scope(
        {
            "api_plan_review": {"decision": "pass"},
            "params": {"force_continue": force_continue},
            "policy": {"force_continue_allowed": allowed},
        }
    )


def test_default_policy_preserves_the_existing_force_continue_bypass() -> None:
    assert evaluate(parse_expression(BYPASS), _bypass_scope(True, True)) is True
    assert evaluate(parse_expression(BYPASS), _bypass_scope(False, True)) is False


def test_policy_can_forbid_the_force_continue_bypass_org_wide() -> None:
    assert evaluate(parse_expression(BYPASS), _bypass_scope(True, False)) is False


def test_schema_has_no_inline_policy_literals() -> None:
    from assurance_agent import resources

    text = resources.read_text("schemas", "workflow-schema.yaml")
    assert "['high','critical']" not in text
    assert "policy.human_review_risk_levels" in text
    assert text.count("params.force_continue == true") == 5
    assert text.count("policy.force_continue_allowed == true") == 5
