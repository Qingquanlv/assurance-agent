from __future__ import annotations

from pathlib import Path

import pytest

from assurance_product.bootstrap.preflight import BootstrapPreflightError, preflight_bootstrap
from tests.product.test_bootstrap_contracts import _spec


def _sut(tmp_path: Path) -> Path:
    project = tmp_path / "sut"
    aa = project / ".aa"
    aa.mkdir(parents=True)
    (aa / "policy.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text("version: 1\n", encoding="utf-8")
    return project


def test_preflight_accepts_clean_workspace(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    preflight_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
    )


def test_preflight_rejects_missing_policy(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    (project / ".aa" / "policy.yaml").unlink()
    with pytest.raises(BootstrapPreflightError, match="policy.yaml"):
        preflight_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=tmp_path / "runs",
            change_id="BOOT-1",
            environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
        )


def test_preflight_allows_unbound_qa_runtime(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    (project / "qa" / ".runtime").mkdir(parents=True)
    preflight_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-1",
        environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
    )


def test_preflight_rejects_foreign_qa(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    qa = project / "qa"
    qa.mkdir()
    (qa / ".qa.yaml").write_text("change:\n  change_id: OTHER\n", encoding="utf-8")
    with pytest.raises(BootstrapPreflightError, match="qa"):
        preflight_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=tmp_path / "runs",
            change_id="BOOT-1",
            environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
        )


def test_preflight_allows_prior_run_qa_when_reusing_directory(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    qa = project / "qa"
    qa.mkdir()
    (qa / ".qa.yaml").write_text("change:\n  change_id: BOOT-PREV\n", encoding="utf-8")
    preflight_bootstrap(
        project_dir=project,
        spec=_spec(),
        runs_root=tmp_path / "runs",
        change_id="BOOT-NEXT",
        environ={"AA_NEXT_OPENCODE_TOKEN": "t", "QA_ADMIN_PASSWORD": "x"},
        reuse_directory=True,
    )


def test_preflight_rejects_ambient_model_override(tmp_path: Path) -> None:
    project = _sut(tmp_path)
    with pytest.raises(BootstrapPreflightError, match="OPENCODE_MODEL"):
        preflight_bootstrap(
            project_dir=project,
            spec=_spec(),
            runs_root=tmp_path / "runs",
            change_id="BOOT-1",
            environ={
                "AA_NEXT_OPENCODE_TOKEN": "t",
                "QA_ADMIN_PASSWORD": "x",
                "OPENCODE_MODEL": "x",
            },
        )
