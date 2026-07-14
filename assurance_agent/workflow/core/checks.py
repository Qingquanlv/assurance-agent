"""Deterministic environment checks behind `aa doctor`."""

import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from assurance_agent.config import (
    CONFIG_RELPATH,
    AaConfig,
    ConfigInvalidError,
    ConfigNotFoundError,
    load_config,
)

CheckStatus = Literal["ok", "warning", "error"]


class CheckResult(BaseModel):
    id: str
    group: str
    status: CheckStatus
    message: str
    suggested_fix: str | None = None


class DoctorResult(BaseModel):
    status: CheckStatus
    checks: list[CheckResult]


def _build(checks: list[CheckResult]) -> DoctorResult:
    status: CheckStatus = "ok"
    if any(c.status == "error" for c in checks):
        status = "error"
    elif any(c.status == "warning" for c in checks):
        status = "warning"
    return DoctorResult(status=status, checks=checks)


def _value_check(check_id: str, ok: bool, ok_msg: str, fix: str) -> CheckResult:
    if ok:
        return CheckResult(id=check_id, group="config", status="ok", message=ok_msg)
    return CheckResult(id=check_id, group="config", status="error", message=fix, suggested_fix=fix)


def _path_check(root: Path, rel: str, check_id: str, group: str, label: str) -> CheckResult:
    if (root / rel).exists():
        return CheckResult(id=check_id, group=group, status="ok", message=f"{label} exists")
    return CheckResult(id=check_id, group=group, status="warning", message=f"{label} not found: {rel}")


def _framework_check(name: str) -> CheckResult:
    if shutil.which(name):
        return CheckResult(
            id=f"framework.{name}", group="frameworks", status="ok", message=f"{name} available"
        )
    return CheckResult(
        id=f"framework.{name}",
        group="frameworks",
        status="warning",
        message=f"{name} not found on PATH",
        suggested_fix=f"Install {name} in the project environment",
    )


def run_doctor_checks(root: Path) -> DoctorResult:
    checks: list[CheckResult] = []
    try:
        cfg = load_config(root)
    except ConfigNotFoundError:
        checks.append(
            CheckResult(
                id="config.exists",
                group="config",
                status="error",
                message=f"{CONFIG_RELPATH} not found",
                suggested_fix="Run `aa init`",
            )
        )
        return _build(checks)
    except ConfigInvalidError as err:
        checks.append(
            CheckResult(id="config.exists", group="config", status="ok", message=f"{CONFIG_RELPATH} found")
        )
        checks.append(CheckResult(id="config.schema", group="config", status="error", message=str(err)))
        return _build(checks)

    checks.append(
        CheckResult(id="config.exists", group="config", status="ok", message=f"{CONFIG_RELPATH} found")
    )
    checks.append(CheckResult(id="config.schema", group="config", status="ok", message="config schema valid"))
    checks.extend(_config_value_checks(cfg))
    checks.extend(_source_and_dir_checks(root, cfg))
    checks.extend(_framework_checks(cfg))
    return _build(checks)


def _config_value_checks(cfg: AaConfig) -> list[CheckResult]:
    return [
        _value_check(
            "config.prd_input_mode",
            cfg.generation.prd_input_mode == "prompt",
            "PRD input mode = prompt",
            'generation.prd_input_mode must be "prompt"',
        ),
        _value_check(
            "config.execution_entry",
            cfg.execution.entry == "cli",
            "execution entry = cli",
            'execution.entry must be "cli"',
        ),
        _value_check(
            "config.self_healing_mode",
            cfg.execution.self_healing.mode == "proposal-only",
            "self-healing mode = proposal-only",
            'execution.self_healing.mode must be "proposal-only"',
        ),
        _value_check(
            "config.e2e_default_pom",
            cfg.generation.e2e.default_pom is False,
            "e2e.default_pom = false",
            "generation.e2e.default_pom must be false",
        ),
    ]


def _source_and_dir_checks(root: Path, cfg: AaConfig) -> list[CheckResult]:
    checks = [
        _path_check(root, cfg.sources.frontend, "sources.frontend", "sources", "frontend path"),
        _path_check(root, cfg.sources.backend, "sources.backend", "sources", "backend path"),
    ]
    dirs = [
        ("dir.qa.cases", cfg.qa.cases, "qa/cases"),
        ("dir.qa.changes", cfg.qa.changes, "qa/changes"),
        ("dir.tests.api", cfg.tests.api, "tests/api"),
        ("dir.tests.e2e", cfg.tests.e2e, "tests/e2e"),
        ("dir.tests.helpers", cfg.tests.helpers, "tests/helpers"),
        ("dir.tests.reports", cfg.tests.reports, "tests/reports"),
    ]
    checks.extend(_path_check(root, rel, cid, "directories", label) for cid, rel, label in dirs)
    return checks


def _framework_checks(cfg: AaConfig) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if cfg.frameworks.api.enabled:
        checks.append(_framework_check(cfg.frameworks.api.name))
    if cfg.frameworks.e2e.enabled:
        checks.append(_framework_check(cfg.frameworks.e2e.name))
    return checks
