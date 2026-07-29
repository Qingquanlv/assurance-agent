"""组织策略常量表：缺文件用打包默认值，非法即 PolicyError（spec C3 / C3b）。"""

from pathlib import Path

import pytest

from assurance_agent.artifacts.policy import POLICY_REL_PATH, PolicyError, load_policy, policy_digest


def _write(root: Path, body: str) -> None:
    (root / ".aa").mkdir(parents=True, exist_ok=True)
    (root / POLICY_REL_PATH).write_text(body, encoding="utf-8")


VALID = """\
version: 1
human_review_risk_levels: [high]
force_continue_allowed: true
plan_checks:
  l1_path: warn
  shared_factory: warn
  assert_ideal: warn
  capability_keys: warn
coverage_floor:
  risk_high: 0.9
  risk_medium: 0.7
fuzz:
  required_when_endpoint_has_auth: true
healing:
  auth_module: require_human
"""

_DEFAULT_EVIDENCE_SUFFICIENCY = {
    "recency_hours": 72,
    "required_kinds": {
        "API": ["covered", "execution_recent"],
        "E2E": ["covered", "execution_recent"],
        "Fuzz": ["covered", "fuzz_run"],
        "Performance": ["covered", "perf_run"],
    },
    "on_insufficient": "require_human",
}


def test_missing_file_falls_back_to_packaged_default(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert policy.version == 1
    assert policy.human_review_risk_levels == ["high", "critical"]
    assert policy.force_continue_allowed is True
    assert policy.plan_checks["assert_ideal"] == "warn"
    assert policy.coverage_floor.risk_high == 0.9
    assert policy.fuzz.required_when_endpoint_has_auth is True
    assert policy.healing.auth_module == "require_human"
    assert policy.evidence_sufficiency.model_dump(mode="json") == _DEFAULT_EVIDENCE_SUFFICIENCY


def test_project_file_overrides_the_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "version: 1\n"
        "human_review_risk_levels: [critical]\n"
        "force_continue_allowed: false\n"
        "plan_checks:\n"
        "  l1_path: warn\n"
        "  shared_factory: warn\n"
        "  assert_ideal: block\n"
        "  capability_keys: require_human\n"
        "coverage_floor:\n"
        "  risk_high: 0.95\n"
        "  risk_medium: 0.8\n"
        "fuzz:\n"
        "  required_when_endpoint_has_auth: false\n"
        "healing:\n"
        "  auth_module: block\n",
    )
    policy = load_policy(tmp_path)
    assert policy.human_review_risk_levels == ["critical"]
    assert policy.force_continue_allowed is False
    assert policy.plan_checks["assert_ideal"] == "block"
    assert policy.coverage_floor.risk_high == 0.95
    assert policy.fuzz.required_when_endpoint_has_auth is False
    assert policy.healing.auth_module == "block"


def test_legacy_plan_check_action_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "version: 1\n"
        "human_review_risk_levels: [high]\n"
        "force_continue_allowed: true\n"
        "plan_check_action: warn\n"
        "plan_checks:\n"
        "  l1_path: warn\n"
        "  shared_factory: warn\n"
        "  assert_ideal: warn\n"
        "  capability_keys: warn\n"
        "coverage_floor: {risk_high: 0.9, risk_medium: 0.7}\n"
        "fuzz: {required_when_endpoint_has_auth: true}\n"
        "healing: {auth_module: require_human}\n",
    )
    with pytest.raises(PolicyError, match="plan_check_action|extra"):
        load_policy(tmp_path)


def test_unknown_plan_check_id_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID.replace("capability_keys: warn", "not_a_check: warn"))
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID.replace("assert_ideal: warn", "assert_ideal: shrug"))
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
    dumped = load_policy(tmp_path).model_dump(mode="json")
    assert isinstance(dumped["human_review_risk_levels"], list)
    assert isinstance(dumped["plan_checks"], dict)


def test_policy_without_evidence_sufficiency_gets_defaulted(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    policy = load_policy(tmp_path)
    assert policy.evidence_sufficiency.model_dump(mode="json") == _DEFAULT_EVIDENCE_SUFFICIENCY


def test_explicit_evidence_sufficiency_is_validated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 24\n"
        "  required_kinds:\n"
        "    API: [covered]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    policy = load_policy(tmp_path)
    assert policy.evidence_sufficiency.recency_hours == 24
    assert policy.evidence_sufficiency.required_kinds["API"] == ["covered"]
    assert policy.evidence_sufficiency.on_insufficient == "warn"


def test_unknown_evidence_kind_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 72\n"
        "  required_kinds:\n"
        "    API: [covered, made_up]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_unknown_on_insufficient_action_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 72\n"
        "  required_kinds:\n"
        "    API: [covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: shrug\n",
    )
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_non_positive_recency_hours_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 0\n"
        "  required_kinds:\n"
        "    API: [covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


def test_digest_changes_when_evidence_sufficiency_changes(tmp_path: Path) -> None:
    baseline = load_policy(tmp_path)
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 48\n"
        "  required_kinds:\n"
        "    API: [covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    assert policy_digest(load_policy(tmp_path)) != policy_digest(baseline)
