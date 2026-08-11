"""组织策略常量表：缺文件用打包默认值，非法即 PolicyError（spec C3 / C3b）。"""

from pathlib import Path

import pytest

import assurance_agent.artifacts.models.assurance as assurance_model
import assurance_agent.artifacts.models.policy as policy_model
from assurance_agent.artifacts.models.assurance import KNOWN_PLAN_CHECK_IDS
from assurance_agent.artifacts.policy import (
    POLICY_REL_PATH,
    PolicyError,
    PolicySnapshot,
    load_policy,
    load_policy_snapshot,
    load_policy_snapshot_bytes,
    normalized_policy_bytes,
    policy_digest,
)


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

# The packaged default spelled out explicitly. Appending it to VALID must be
# indistinguishable from omitting it (the field-level default migration path).
EVIDENCE_SUFFICIENCY = """\
evidence_sufficiency:
  recency_hours: 72
  required_kinds:
    API: [covered, execution_recent]
    E2E: [covered, execution_recent]
    Fuzz: [covered, fuzz_run]
    Performance: [covered, perf_run]
  on_insufficient: require_human
  floors:
    low:
      constraint_coverage: {target: value, min: 0.5}
      auth_matrix_coverage: {target: touched, min: 1.0}
    medium:
      constraint_coverage: {target: value, min: 0.7}
      auth_matrix_coverage: {target: touched, min: 1.0}
      journey_coverage: {target: touched, min: 1.0}
    high:
      constraint_coverage: {target: touched, min: 1.0}
      auth_matrix_coverage: {target: touched, min: 1.0}
      journey_coverage: {target: touched, min: 1.0}
    critical:
      constraint_coverage: {target: touched, min: 1.0}
      auth_matrix_coverage: {target: touched, min: 1.0}
      journey_coverage: {target: touched, min: 1.0}
      adversarial_clean: {target: holds, must_hold: true}
  cadence:
    pr: [diff_coverage, constraint_coverage, auth_matrix_coverage, journey_coverage, threshold_slack]
    nightly: [mutation_score, assertion_strength, adversarial_yield, baseline_drift]
  mutation_budget_seconds: 300
"""


def test_missing_file_falls_back_to_packaged_default(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert policy.version == 1
    assert policy.human_review_risk_levels == ["high", "critical"]
    assert policy.force_continue_allowed is True
    assert policy.plan_checks["assert_ideal"] == "warn"
    assert policy.coverage_floor.risk_high == 0.9
    assert policy.fuzz.required_when_endpoint_has_auth is True
    assert policy.healing.auth_module == "require_human"
    assert policy.evidence_sufficiency.recency_hours == 72
    assert policy.evidence_sufficiency.required_kinds["API"] == ["covered", "execution_recent"]
    assert policy.evidence_sufficiency.required_kinds["Fuzz"] == ["covered", "fuzz_run"]
    assert policy.evidence_sufficiency.required_kinds["Performance"] == ["covered", "perf_run"]
    assert policy.evidence_sufficiency.on_insufficient == "require_human"
    assert policy.evidence_sufficiency.mutation_budget_seconds == 300
    assert policy.evidence_sufficiency.floors["low"]["constraint_coverage"].min == 0.5
    assert policy.evidence_sufficiency.floors["low"]["constraint_coverage"].target == "value"
    assert policy.evidence_sufficiency.floors["critical"]["adversarial_clean"].must_hold is True
    assert "mutation_score" not in {
        key for band in policy.evidence_sufficiency.floors.values() for key in band
    }
    assert policy.evidence_sufficiency.cadence.pr == [
        "diff_coverage",
        "constraint_coverage",
        "auth_matrix_coverage",
        "journey_coverage",
        "threshold_slack",
    ]
    assert policy.evidence_sufficiency.cadence.nightly == [
        "mutation_score",
        "assertion_strength",
        "adversarial_yield",
        "baseline_drift",
    ]


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


def test_policy_predating_evidence_sufficiency_takes_the_packaged_default(tmp_path: Path) -> None:
    """C3b-era files have no evidence_sufficiency block; they must still load."""
    _write(tmp_path, VALID)
    assert load_policy(tmp_path).evidence_sufficiency == load_policy(tmp_path / "absent").evidence_sufficiency


def test_explicit_evidence_sufficiency_overrides_the_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID
        + EVIDENCE_SUFFICIENCY.replace("recency_hours: 72", "recency_hours: 6")
        .replace("API: [covered, execution_recent]", "API: [covered, execution_recent, pass_status]")
        .replace("on_insufficient: require_human", "on_insufficient: warn"),
    )
    sufficiency = load_policy(tmp_path).evidence_sufficiency
    assert sufficiency.recency_hours == 6
    assert sufficiency.required_kinds["API"] == ["covered", "execution_recent", "pass_status"]
    assert sufficiency.on_insufficient == "warn"


def test_unknown_evidence_kind_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path, VALID + EVIDENCE_SUFFICIENCY.replace("Fuzz: [covered, fuzz_run]", "Fuzz: [covered, vibes]")
    )
    with pytest.raises(PolicyError, match="vibes"):
        load_policy(tmp_path)


def test_unknown_case_type_is_rejected(tmp_path: Path) -> None:
    """All four real keys stay present, so only the extra key can be the cause."""
    _write(
        tmp_path,
        VALID
        + EVIDENCE_SUFFICIENCY.replace(
            "    Performance: [covered, perf_run]\n",
            "    Performance: [covered, perf_run]\n    Chaos: [covered]\n",
        ),
    )
    with pytest.raises(PolicyError, match="Chaos"):
        load_policy(tmp_path)


def test_missing_case_type_is_rejected(tmp_path: Path) -> None:
    """A silently absent case type would make its rows vacuously sufficient."""
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY.replace("    Performance: [covered, perf_run]\n", ""))
    with pytest.raises(PolicyError, match="Performance"):
        load_policy(tmp_path)


@pytest.mark.parametrize("recency", ["0", "-1"])
def test_non_positive_recency_hours_is_rejected(tmp_path: Path, recency: str) -> None:
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY.replace("recency_hours: 72", f"recency_hours: {recency}"))
    with pytest.raises(PolicyError, match="greater than 0"):
        load_policy(tmp_path)


def test_unknown_on_insufficient_action_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + EVIDENCE_SUFFICIENCY.replace("on_insufficient: require_human", "on_insufficient: shrug"),
    )
    with pytest.raises(PolicyError, match="shrug"):
        load_policy(tmp_path)


def test_unknown_evidence_sufficiency_key_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY + "  grace_period_hours: 1\n")
    with pytest.raises(PolicyError, match="grace_period_hours"):
        load_policy(tmp_path)


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


def test_digest_covers_evidence_sufficiency(tmp_path: Path) -> None:
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY)
    baseline = policy_digest(load_policy(tmp_path))
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY.replace("recency_hours: 72", "recency_hours: 24"))
    assert policy_digest(load_policy(tmp_path)) != baseline


def test_digest_of_an_omitted_block_equals_the_explicit_default(tmp_path: Path) -> None:
    """Migration is digest-neutral for files that keep the default disposition."""
    _write(tmp_path, VALID)
    defaulted = policy_digest(load_policy(tmp_path))
    _write(tmp_path, VALID + EVIDENCE_SUFFICIENCY)
    assert policy_digest(load_policy(tmp_path)) == defaulted


def test_risk_levels_dump_to_a_plain_list_for_the_dsl(tmp_path: Path) -> None:
    """gate DSL 的 `in` 运算符要求右操作数是 list（dsl.py:349）。"""
    dumped = load_policy(tmp_path).model_dump(mode="json")
    assert isinstance(dumped["human_review_risk_levels"], list)
    assert isinstance(dumped["plan_checks"], dict)


def test_defaulted_evidence_sufficiency_is_part_of_the_digest_payload(tmp_path: Path) -> None:
    """policy_digest hashes the dump, so defaults must survive serialization."""
    _write(tmp_path, VALID)
    dumped = load_policy(tmp_path).model_dump(mode="json")
    assert dumped["evidence_sufficiency"]["recency_hours"] == 72
    assert isinstance(dumped["evidence_sufficiency"]["required_kinds"]["API"], list)
    assert dumped["evidence_sufficiency"]["mutation_budget_seconds"] == 300
    assert dumped["evidence_sufficiency"]["floors"]["high"]["journey_coverage"]["target"] == "touched"
    assert dumped["evidence_sufficiency"]["cadence"]["pr"][0] == "diff_coverage"


def test_unknown_floor_metric_key_is_rejected(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "constraint_coverage: {target: value, min: 0.5}",
        "constraint_coverage_touched: {target: value, min: 0.5}",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="constraint_coverage_touched"):
        load_policy(tmp_path)


def test_unknown_cadence_metric_key_is_rejected(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "pr: [diff_coverage, constraint_coverage, auth_matrix_coverage, journey_coverage, threshold_slack]",
        "pr: [diff_coverage, not_a_metric]",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="not_a_metric"):
        load_policy(tmp_path)


def test_floor_target_must_match_metric_shape(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "adversarial_clean: {target: holds, must_hold: true}",
        "adversarial_clean: {target: value, min: 1.0}",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="boolean|holds"):
        load_policy(tmp_path)


def test_floor_requires_exactly_one_of_min_or_must_hold(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "constraint_coverage: {target: value, min: 0.5}",
        "constraint_coverage: {target: value, min: 0.5, must_hold: true}",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="exactly one"):
        load_policy(tmp_path)


def test_missing_floor_tier_is_rejected(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "    critical:\n"
        "      constraint_coverage: {target: touched, min: 1.0}\n"
        "      auth_matrix_coverage: {target: touched, min: 1.0}\n"
        "      journey_coverage: {target: touched, min: 1.0}\n"
        "      adversarial_clean: {target: holds, must_hold: true}\n",
        "",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="critical"):
        load_policy(tmp_path)


def test_assertion_strength_may_name_a_surface(tmp_path: Path) -> None:
    """Optional per-surface floor for the multi-surface metric; no new metric key."""
    extra = EVIDENCE_SUFFICIENCY.replace(
        "    medium:\n"
        "      constraint_coverage: {target: value, min: 0.7}\n"
        "      auth_matrix_coverage: {target: touched, min: 1.0}\n"
        "      journey_coverage: {target: touched, min: 1.0}\n",
        "    medium:\n"
        "      constraint_coverage: {target: value, min: 0.7}\n"
        "      auth_matrix_coverage: {target: touched, min: 1.0}\n"
        "      journey_coverage: {target: touched, min: 1.0}\n"
        "      assertion_strength: {target: value, min: 0.5, surface: api}\n",
    )
    _write(tmp_path, VALID + extra)
    floor = load_policy(tmp_path).evidence_sufficiency.floors["medium"]["assertion_strength"]
    assert floor.surface == "api"
    assert floor.min == 0.5


def test_surface_on_a_single_surface_metric_is_rejected(tmp_path: Path) -> None:
    bad = EVIDENCE_SUFFICIENCY.replace(
        "constraint_coverage: {target: value, min: 0.5}",
        "constraint_coverage: {target: value, min: 0.5, surface: api}",
    )
    _write(tmp_path, VALID + bad)
    with pytest.raises(PolicyError, match="multi-surface|surface"):
        load_policy(tmp_path)


def test_non_positive_mutation_budget_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + EVIDENCE_SUFFICIENCY.replace("mutation_budget_seconds: 300", "mutation_budget_seconds: 0"),
    )
    with pytest.raises(PolicyError, match="greater than 0"):
        load_policy(tmp_path)

def test_differently_formatted_yaml_yields_identical_normalized_bytes_and_digest(
    tmp_path: Path,
) -> None:
    block_style = VALID
    flow_style = (
        "version: 1\n"
        "human_review_risk_levels: [high]\n"
        "force_continue_allowed: true\n"
        "plan_checks: {l1_path: warn, shared_factory: warn, assert_ideal: warn, "
        "capability_keys: warn}\n"
        "coverage_floor: {risk_high: 0.9, risk_medium: 0.7}\n"
        "fuzz: {required_when_endpoint_has_auth: true}\n"
        "healing: {auth_module: require_human}\n"
    )
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a, block_style)
    _write(root_b, flow_style)
    snap_a = load_policy_snapshot(root_a)
    snap_b = load_policy_snapshot(root_b)
    assert snap_a.canonical_bytes == snap_b.canonical_bytes
    assert snap_a.digest == snap_b.digest
    assert snap_a.digest == policy_digest(snap_a.policy)
    assert snap_a.canonical_bytes == normalized_policy_bytes(snap_a.policy)
    assert snap_a.canonical_bytes.endswith(b"\n")

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

def test_duplicate_required_kinds_within_case_type_are_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 72\n"
        "  required_kinds:\n"
        "    API: [covered, covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "    Performance: [covered, perf_run]\n"
        "  on_insufficient: warn\n",
    )
    with pytest.raises(PolicyError, match="duplicate"):
        load_policy(tmp_path)

def test_equal_project_and_default_content_share_bytes_but_distinct_origins(
    tmp_path: Path,
) -> None:
    from assurance_agent import resources

    default_snap = load_policy_snapshot(tmp_path)
    assert default_snap.origin == "packaged_default"
    _write(tmp_path, resources.read_text("schemas", "policy-default.yaml"))
    project_snap = load_policy_snapshot(tmp_path)
    assert project_snap.origin == "project"
    assert project_snap.canonical_bytes == default_snap.canonical_bytes
    assert project_snap.digest == default_snap.digest

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

def test_load_policy_snapshot_bytes_accepts_explicit_origin() -> None:
    snap = load_policy_snapshot_bytes(None, origin="packaged_default")
    assert snap.origin == "packaged_default"
    assert isinstance(snap, PolicySnapshot)

def test_missing_required_kinds_key_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        VALID + "evidence_sufficiency:\n"
        "  recency_hours: 72\n"
        "  required_kinds:\n"
        "    API: [covered, execution_recent]\n"
        "    E2E: [covered, execution_recent]\n"
        "    Fuzz: [covered, fuzz_run]\n"
        "  on_insufficient: warn\n",
    )
    with pytest.raises(PolicyError, match="required_kinds missing keys"):
        load_policy(tmp_path)

def test_policy_reexports_the_shared_catalog_object() -> None:
    assert policy_model.KNOWN_PLAN_CHECK_IDS is assurance_model.KNOWN_PLAN_CHECK_IDS

def test_policy_requires_exact_shared_check_catalog(tmp_path: Path) -> None:
    policy = load_policy(tmp_path)
    assert set(policy.plan_checks) == KNOWN_PLAN_CHECK_IDS

def test_policy_without_evidence_sufficiency_gets_defaulted(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    policy = load_policy(tmp_path)
    assert policy.evidence_sufficiency.model_dump(mode="json") == _DEFAULT_EVIDENCE_SUFFICIENCY

def test_snapshot_is_immutable_against_future_packaged_default_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = load_policy_snapshot(tmp_path)
    before_bytes = snap.canonical_bytes
    before_digest = snap.digest
    before_policy = snap.policy

    def mutated_default(*_a: object, **_k: object) -> str:
        return (
            "version: 1\n"
            "human_review_risk_levels: [critical]\n"
            "force_continue_allowed: true\n"
            "plan_checks:\n"
            "  l1_path: warn\n"
            "  shared_factory: warn\n"
            "  assert_ideal: warn\n"
            "  capability_keys: warn\n"
            "coverage_floor:\n"
            "  risk_high: 0.9\n"
            "  risk_medium: 0.7\n"
            "fuzz:\n"
            "  required_when_endpoint_has_auth: true\n"
            "healing:\n"
            "  auth_module: require_human\n"
        )

    monkeypatch.setattr(
        "assurance_agent.artifacts.policy.resources.read_text",
        mutated_default,
    )
    assert load_policy_snapshot(tmp_path).digest != before_digest
    assert snap.canonical_bytes == before_bytes
    assert snap.digest == before_digest
    assert snap.policy == before_policy
