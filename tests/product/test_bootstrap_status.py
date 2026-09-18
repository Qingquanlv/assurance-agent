from __future__ import annotations

import os
from pathlib import Path

import pytest

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.status import (
    derive_bootstrap_change_id,
    find_active_run,
    read_bootstrap_status,
    run_dir_for,
    write_bootstrap_status,
    write_effective_spec,
    write_run_manifest,
)
from tests.product.test_bootstrap_contracts import _spec


def test_change_id_is_path_safe() -> None:
    change_id = derive_bootstrap_change_id(stamp="20260918-120000", nonce="abcd1234")
    assert change_id == "BOOT-20260918-120000-abcd1234"
    with pytest.raises(ValueError):
        derive_bootstrap_change_id(stamp="bad stamp", nonce="x")


def test_status_round_trip(tmp_path: Path) -> None:
    run_dir = run_dir_for(tmp_path, "BOOT-1")
    status = BootstrapStatusV1(phase="preparing", change_id="BOOT-1")
    write_bootstrap_status(run_dir, status)
    loaded = read_bootstrap_status(run_dir)
    assert loaded == status
    assert (run_dir / "bootstrap-status.json").is_file()


def test_effective_spec_is_yaml(tmp_path: Path) -> None:
    run_dir = run_dir_for(tmp_path, "BOOT-1")
    path = write_effective_spec(run_dir, _spec())
    assert path.name == "run-spec.effective.yaml"
    assert "assurance-opencode" in path.read_text(encoding="utf-8")


def test_find_active_run_requires_live_pid(tmp_path: Path) -> None:
    project = tmp_path / "sut"
    project.mkdir()
    run_dir = run_dir_for(tmp_path / "runs", "BOOT-1")
    write_run_manifest(run_dir, {"project_dir": str(project.resolve())})
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(
            phase="running",
            change_id="BOOT-1",
            opencode={"endpoint": "http://127.0.0.1:4100", "pid": os.getpid()},
        ),
    )
    assert find_active_run(tmp_path / "runs", project) == run_dir.resolve()
    write_bootstrap_status(run_dir, BootstrapStatusV1(phase="terminal", change_id="BOOT-1"))
    assert find_active_run(tmp_path / "runs", project) is None
