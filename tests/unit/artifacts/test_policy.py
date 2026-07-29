"""组织策略常量表：缺文件用打包默认值，非法即 PolicyError（spec C3）。"""

from pathlib import Path

import pytest

from assurance_agent.artifacts.policy import POLICY_REL_PATH, PolicyError, load_policy, policy_digest


def _write(root: Path, body: str) -> None:
    (root / ".aa").mkdir(parents=True, exist_ok=True)
    (root / POLICY_REL_PATH).write_text(body, encoding="utf-8")


VALID = (
    "version: 1\nhuman_review_risk_levels: [high]\nforce_continue_allowed: true\nplan_check_action: warn\n"
)


def test_missing_file_falls_back_to_packaged_default(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert policy.version == 1
    assert policy.human_review_risk_levels == ["high", "critical"]
    assert policy.force_continue_allowed is True
    assert policy.plan_check_action == "warn"


def test_project_file_overrides_the_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "version: 1\n"
        "human_review_risk_levels: [critical]\n"
        "force_continue_allowed: false\n"
        "plan_check_action: block\n",
    )
    policy = load_policy(tmp_path)
    assert policy.human_review_risk_levels == ["critical"]
    assert policy.force_continue_allowed is False
    assert policy.plan_check_action == "block"


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID.replace("plan_check_action: warn", "plan_check_action: shrug"))
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID + "deploy_on_friday: true\n")
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "version: [unclosed\n")
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_digest_is_stable_and_content_addressed(tmp_path: Path) -> None:
    default = load_policy(tmp_path)
    assert policy_digest(default) == policy_digest(load_policy(tmp_path))
    _write(tmp_path, VALID.replace("[high]", "[critical]"))
    assert policy_digest(load_policy(tmp_path)) != policy_digest(default)


def test_risk_levels_dump_to_a_plain_list_for_the_dsl(tmp_path: Path) -> None:
    """gate DSL 的 `in` 运算符要求右操作数是 list（dsl.py:349）。"""
    assert isinstance(load_policy(tmp_path).model_dump(mode="json")["human_review_risk_levels"], list)
