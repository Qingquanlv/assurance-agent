from __future__ import annotations

from pathlib import Path

import pytest

from tests.product.cli_support import SECRET_ENV, SECRET_VALUE

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_cutover_validator_and_runtime_selector_are_gone() -> None:
    import importlib.util

    from assurance_product import application
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert importlib.util.find_spec("assurance_product.runtime_selection") is None
    assert not hasattr(application, "select_runtime")
    assert not hasattr(application, "use_test_runtime_selector")
    assert not hasattr(application, "validate_entrypoint_runtime_cutover")
    assert set(PRODUCT_ENTRYPOINTS) == set(application.ENTRYPOINT_AGENT_CONTRACT_IDS)


def test_absent_legacy_evidence_without_marker_fails_closed(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("seed\n", encoding="utf-8")
    ChangeWorkspace.prepare(project_dir, "CH-ABSENT-001")
    result = cli_runner.invoke(
        app,
        [
            "status",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            "CH-ABSENT-001",
            "--invocation-id",
            "inv-absent-001",
            "--product",
            "assurance-opencode",
            "--binding-dist",
            installed_sources.deployments["opencode"].distribution,
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            installed_sources.deployments["opencode"].declaration_path,
            "--config-tree",
            str(installed_sources.configuration_tree.path),
            "--secret",
            f"opencode.token=env:{SECRET_ENV}",
        ],
    )
    assert result.exit_code == 40, result.output
