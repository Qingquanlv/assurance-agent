from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graph_engine.attempts.models.resolutions import IndeterminateTaskResult, PermanentTaskFailure
from graph_engine.attempts.resources.workspace import TaskWorkspaceViolation

_HELPER_SPEC = importlib.util.spec_from_file_location(
    "test_kernel_raw_agent_recovery",
    Path(__file__).with_name("test_kernel_raw_agent_recovery.py"),
)
assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(_HELPERS)
_SECRET = _HELPERS._SECRET
_build = _HELPERS._build


@pytest.mark.parametrize(
    "fault",
    [
        "unauthorized_write",
        "symlink_escape",
        "project_instruction_discovery",
        "secret_leakage",
        "malformed_terminal",
        "result_file_disagreement",
    ],
)
async def test_kernel_raw_security_faults_fail_before_promote(tmp_path: Path, fault: str) -> None:
    kernel, key, resolved, validated, context, _executor, runtime, workspace, project, store, _finalize = (
        _build(tmp_path, fault=fault)
    )
    try:
        try:
            result = await kernel.execute_or_recover(key, resolved, validated, context)
        except (TaskWorkspaceViolation, ValueError):
            result = None
        assert workspace.promotions == 0
        assert not (project / "out.txt").exists() or fault == "symlink_escape"
        if result is not None:
            assert isinstance(result, (PermanentTaskFailure, IndeterminateTaskResult))
            assert getattr(result, "writes_promoted", False) is False
        if fault == "secret_leakage":
            snapshot = await kernel.checkpoints.load(key)
            assert snapshot is not None
            encoded = str(snapshot.activity_reference) + str(snapshot.terminal)
            assert _SECRET not in encoded
            assert "transcript" not in str(snapshot.activity_reference)
            assert runtime.prompt_admissions <= 1
    finally:
        store.close()
