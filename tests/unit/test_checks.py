import shutil
from pathlib import Path

from assurance_agent.workflow.core.checks import run_doctor_checks
from assurance_agent.workflow.core.generator import generate_project
from assurance_agent.workflow.core.templates import InitAnswers


def check_ids(result) -> dict[str, str]:
    return {c.id: c.status for c in result.checks}


def test_missing_config_is_single_error(tmp_path: Path) -> None:
    result = run_doctor_checks(tmp_path)
    assert result.status == "error"
    assert check_ids(result) == {"config.exists": "error"}


def test_fresh_scaffold_reports_config_ok_sources_warning(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    result = run_doctor_checks(tmp_path)
    ids = check_ids(result)
    assert ids["config.exists"] == "ok"
    assert ids["config.schema"] == "ok"
    assert ids["config.prd_input_mode"] == "ok"
    assert ids["config.execution_entry"] == "ok"
    assert ids["config.self_healing_mode"] == "ok"
    assert ids["config.e2e_default_pom"] == "ok"
    # ./frontend and ./backend don't exist -> warning, not error
    assert ids["sources.frontend"] == "warning"
    assert ids["sources.backend"] == "warning"
    assert ids["dir.qa.cases"] == "ok"
    assert result.status in ("ok", "warning")


def test_invalid_config_value_is_error(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    config = tmp_path / ".aa/config.yaml"
    config.write_text(config.read_text().replace("entry: cli", "entry: manual"))
    result = run_doctor_checks(tmp_path)
    assert check_ids(result)["config.execution_entry"] == "error"
    assert result.status == "error"


def test_framework_check_reflects_binary_presence(tmp_path: Path) -> None:
    generate_project(tmp_path, InitAnswers())
    result = run_doctor_checks(tmp_path)
    ids = check_ids(result)
    expected = "ok" if shutil.which("pytest") else "warning"
    assert ids["framework.pytest"] == expected
