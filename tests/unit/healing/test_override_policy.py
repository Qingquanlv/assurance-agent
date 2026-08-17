import json
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.healing.override_policy import (
    assert_test_changes_override_allowed,
    load_test_changes_override_policy,
)
from assurance_agent.workflow.healing.safety import HealingGuardError
from assurance_agent.workflow.healing.safety import TestTreeIntegrity as _TestTreeIntegrity

_CHANGED_API = _TestTreeIntegrity(tests_changed=True, changed_files=["tests/api/test_x.py"])


def _write_policy(project_root: Path, healing: dict) -> None:
    policy_path = project_root / ".aa" / "execution-policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps({"healing": healing}, indent=2), encoding="utf-8")


def _write_manifest(change_dir: Path, final_status: str) -> None:
    manifest = change_dir / "execution" / "execution-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"batch_id": "b1", "final_status": final_status}), encoding="utf-8")


def _allow_event(change_dir: Path) -> None:
    append_event_strict(
        change_dir,
        {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": "test-tree-guard",
            "action": "allow_test_changes",
            "reason": "manual fix",
            "who": "cli",
        },
    )


def test_missing_policy_file_defaults_to_with_evidence(tmp_path: Path) -> None:
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.mode == "with-evidence"
    assert policy.evidence is True
    assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)  # 缺省放行（但要求证据）


def test_policy_without_healing_key_defaults(tmp_path: Path) -> None:
    _write_policy(tmp_path, {})
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.mode == "with-evidence"
    assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_malformed_policy_file_defaults(tmp_path: Path) -> None:
    policy_path = tmp_path / ".aa" / "execution-policy.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("{not json", encoding="utf-8")
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.mode == "with-evidence"
    assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_forbidden_string_rejects(tmp_path: Path) -> None:
    _write_policy(tmp_path, {"testChangesOverride": "forbidden"})
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.mode == "forbidden"
    with pytest.raises(HealingGuardError, match="ALLOW-TEST-CHANGES-FORBIDDEN"):
        assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_forbidden_dict_mode_rejects(tmp_path: Path) -> None:
    _write_policy(tmp_path, {"testChangesOverride": {"mode": "forbidden"}})
    policy = load_test_changes_override_policy(tmp_path)
    with pytest.raises(HealingGuardError, match="ALLOW-TEST-CHANGES-FORBIDDEN"):
        assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_free_mode_allows_and_disables_evidence(tmp_path: Path) -> None:
    _write_policy(tmp_path, {"testChangesOverride": "free"})
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.mode == "free"
    assert policy.evidence is False
    assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_conditional_allows_within_allowed_path_globs(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {"testChangesOverride": {"mode": "conditional", "allowedPathGlobs": ["tests/api/**"]}},
    )
    policy = load_test_changes_override_policy(tmp_path)
    assert policy.allowed_path_globs == ("tests/api/**",)
    assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_conditional_denies_outside_allowed_path_globs(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {"testChangesOverride": {"mode": "conditional", "allowedPathGlobs": ["tests/api/**"]}},
    )
    policy = load_test_changes_override_policy(tmp_path)
    integrity = _TestTreeIntegrity(tests_changed=True, changed_files=["tests/e2e/test_y.py"])
    with pytest.raises(HealingGuardError, match="TEST-CHANGES-OVERRIDE-PATH-DENIED"):
        assert_test_changes_override_allowed(tmp_path, integrity, policy)


def test_glob_single_star_does_not_cross_directories(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {"testChangesOverride": {"mode": "conditional", "allowedPathGlobs": ["tests/*"]}},
    )
    policy = load_test_changes_override_policy(tmp_path)
    with pytest.raises(HealingGuardError, match="TEST-CHANGES-OVERRIDE-PATH-DENIED"):
        assert_test_changes_override_allowed(tmp_path, _CHANGED_API, policy)


def test_conditional_without_test_changes_passes(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {"testChangesOverride": {"mode": "conditional", "forbidAfterFail": True}},
    )
    policy = load_test_changes_override_policy(tmp_path)
    unchanged = _TestTreeIntegrity(tests_changed=False, changed_files=[])
    assert_test_changes_override_allowed(tmp_path, unchanged, policy)


def test_forbid_after_fail_rejects_when_last_run_failed(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _write_manifest(change_dir, "FAIL")
    _write_policy(tmp_path, {"testChangesOverride": {"mode": "conditional", "forbidAfterFail": True}})
    policy = load_test_changes_override_policy(tmp_path)
    with pytest.raises(HealingGuardError, match="TEST-CHANGES-OVERRIDE-FORBIDDEN-AFTER-FAIL"):
        assert_test_changes_override_allowed(change_dir, _CHANGED_API, policy)


def test_forbid_after_fail_allows_when_last_run_passed(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _write_manifest(change_dir, "PASS")
    _write_policy(tmp_path, {"testChangesOverride": {"mode": "conditional", "forbidAfterFail": True}})
    policy = load_test_changes_override_policy(tmp_path)
    assert_test_changes_override_allowed(change_dir, _CHANGED_API, policy)


def test_max_overrides_per_change_rejects_at_limit(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _allow_event(change_dir)
    _write_policy(tmp_path, {"testChangesOverride": {"mode": "conditional", "maxOverridesPerChange": 1}})
    policy = load_test_changes_override_policy(tmp_path)
    with pytest.raises(HealingGuardError, match=r"TEST-CHANGES-OVERRIDE-LIMIT-REACHED.*limit 1"):
        assert_test_changes_override_allowed(change_dir, _CHANGED_API, policy)


def test_max_overrides_per_change_allows_below_limit(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _allow_event(change_dir)
    _write_policy(tmp_path, {"testChangesOverride": {"mode": "conditional", "maxOverridesPerChange": 2}})
    policy = load_test_changes_override_policy(tmp_path)
    assert_test_changes_override_allowed(change_dir, _CHANGED_API, policy)
