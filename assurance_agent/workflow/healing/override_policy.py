"""testChangesOverride 执行策略：读取 `.aa/execution-policy.json` 并强制执行。

移植自 TS 版 `test_changes_override.ts` 的 policy 层（loadTestChangesOverridePolicy /
assertTestChangesOverridePolicyAllows）。TS 版的一次性 override token 机制在 m5 P1
修订中被有意再设计为 `aa run --allow-test-changes --rerun-reason` + override
evidence，因此这里只移植策略读取与授权执行层，不移植 token。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.healing.safety import HealingGuardError, TestTreeIntegrity

POLICY_REL_PATH = Path(".aa") / "execution-policy.json"

TestChangesOverrideMode = Literal["forbidden", "with-evidence", "free", "conditional"]

# 对齐 TS 版缺省：策略文件缺失/损坏/无该配置键时默认 with-evidence（放行但要求证据）。
_DEFAULT_MODE: TestChangesOverrideMode = "with-evidence"


@dataclass(frozen=True)
class TestChangesOverridePolicy:
    """归一化后的 `healing.testChangesOverride` 配置（字段与默认值对齐 TS normalizePolicy）。"""

    mode: TestChangesOverrideMode = _DEFAULT_MODE
    evidence: bool = True
    forbid_after_fail: bool = False
    max_overrides_per_change: int | None = None
    allowed_path_globs: tuple[str, ...] = ()


def load_test_changes_override_policy(project_root: Path) -> TestChangesOverridePolicy:
    """读取 `<project>/.aa/execution-policy.json` 的 `healing.testChangesOverride`。"""
    path = project_root / POLICY_REL_PATH
    if not path.is_file():
        return TestChangesOverridePolicy()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TestChangesOverridePolicy()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("healing"), dict):
        return TestChangesOverridePolicy()
    return _normalize_policy(parsed["healing"].get("testChangesOverride"))


def assert_test_changes_override_allowed(
    change_dir: Path,
    integrity: TestTreeIntegrity,
    policy: TestChangesOverridePolicy,
) -> None:
    """对已变更的测试树执行 override 策略，违规时抛 HealingGuardError（错误码对齐 TS 版）。"""
    if policy.mode == "forbidden":
        raise HealingGuardError(
            "ALLOW-TEST-CHANGES-FORBIDDEN: execution policy forbids test changes outside healing"
        )
    if not integrity.tests_changed or policy.mode != "conditional":
        return
    if policy.forbid_after_fail and _latest_final_status(change_dir) == "FAIL":
        raise HealingGuardError(
            "TEST-CHANGES-OVERRIDE-FORBIDDEN-AFTER-FAIL: failed execution batches must enter the healing loop"
        )
    if policy.max_overrides_per_change is not None:
        count = _count_test_change_overrides(change_dir)
        if count >= policy.max_overrides_per_change:
            raise HealingGuardError(
                f"TEST-CHANGES-OVERRIDE-LIMIT-REACHED: {count} overrides already used "
                f"(limit {policy.max_overrides_per_change})"
            )
    if policy.allowed_path_globs:
        denied = [f for f in integrity.changed_files if not _matches_any_glob(f, policy.allowed_path_globs)]
        if denied:
            raise HealingGuardError(
                "TEST-CHANGES-OVERRIDE-PATH-DENIED: changed test files outside allowedPathGlobs "
                f"({', '.join(denied)})"
            )


def _normalize_policy(value: object) -> TestChangesOverridePolicy:
    if value == "forbidden":
        return TestChangesOverridePolicy(mode="forbidden")
    if value == "free":
        return TestChangesOverridePolicy(mode="free", evidence=False)
    if value is None or value == "with-evidence":
        return TestChangesOverridePolicy()
    if isinstance(value, dict):
        raw_mode = value.get("mode")
        mode: TestChangesOverrideMode = (
            raw_mode if raw_mode in ("forbidden", "free", "with-evidence", "conditional") else _DEFAULT_MODE
        )
        globs = value.get("allowedPathGlobs")
        normalized_globs = (
            tuple(g.strip() for g in globs if isinstance(g, str) and g.strip())
            if isinstance(globs, list)
            else ()
        )
        max_overrides = value.get("maxOverridesPerChange")
        return TestChangesOverridePolicy(
            mode=mode,
            evidence=mode != "free",
            forbid_after_fail=value.get("forbidAfterFail") is True,
            max_overrides_per_change=(
                max_overrides
                if isinstance(max_overrides, int) and not isinstance(max_overrides, bool)
                else None
            ),
            allowed_path_globs=normalized_globs,
        )
    return TestChangesOverridePolicy()


def _latest_final_status(change_dir: Path) -> str | None:
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    if not manifest_path.is_file():
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    status = raw.get("final_status")
    return status if status in ("PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED") else None


def _count_test_change_overrides(change_dir: Path) -> int:
    """统计本 change 已记录的 allow_test_changes 人工决定（对齐 TS 版按 decide 事件计数）。"""
    return sum(
        1
        for event in read_events(change_dir)
        if event.get("source") == "decide"
        and event.get("type") == "human_decision"
        and event.get("action") == "allow_test_changes"
    )


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    # 与 risk/context.py::_glob_to_regex 同一写法；import-linter 分层中 risk 在
    # workflow 之上，不能反向 import，故在此复制该惯例（minimatch 的 **/* 语义）。
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches_any_glob(path: str, globs: tuple[str, ...]) -> bool:
    norm = path.replace("\\", "/")
    return any(_glob_to_regex(glob).match(norm) for glob in globs)
