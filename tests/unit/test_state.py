import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import PhaseState, WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.state import (
    StateIntegrityError,
    configure_workflow_params,
    read_state,
    state_guard,
    write_state,
)


def test_roundtrip_returns_workflowstate(tmp_path: Path):
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"explore": {"status": "done"}}}))
    got = read_state(tmp_path)
    assert isinstance(got, WorkflowState)
    assert got.phases.model_extra is not None
    assert got.phases.model_extra["explore"]["status"] == "done"
    # 元数据键不进入模型
    assert "_integrity" not in (got.model_extra or {})


def test_known_state_fields_are_typed_and_roundtrip(tmp_path: Path):
    st = read_state(tmp_path)  # 空态
    st.phases.skill_registry_check = PhaseState(status="pass")
    st.phases.healing.status = "resolved"
    st.phases.healing.attempts_used = 1
    st.gates.healing_available = True
    write_state(tmp_path, st)
    back = read_state(tmp_path)
    assert back.phases.skill_registry_check is not None
    assert back.phases.skill_registry_check.status == "pass"
    assert back.phases.healing.status == "resolved"
    assert back.phases.healing.attempts_used == 1
    assert back.gates.healing_available is True


def test_missing_returns_empty_model(tmp_path: Path):
    st = read_state(tmp_path)
    assert isinstance(st, WorkflowState)
    assert st.phases.healing.attempts_used == 0


def test_tamper_detected(tmp_path: Path):
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"a": {"status": "done"}}}))
    f = tmp_path / "workflow-state.json"
    doc = yaml.safe_load(f.read_text())
    doc["phases"]["a"]["status"] = "FORGED"  # 篡改但不更新哈希
    f.write_text(json.dumps(doc))
    with pytest.raises(StateIntegrityError):
        read_state(tmp_path)


def test_legacy_without_hash_tolerated(tmp_path: Path):
    f = tmp_path / "workflow-state.json"
    f.write_text(json.dumps({"phases": {"a": {"status": "done"}}}))  # 无 _integrity
    got = read_state(tmp_path)
    assert got.phases.model_extra is not None
    assert got.phases.model_extra["a"]["status"] == "done"


def test_state_guard_changes_with_content(tmp_path: Path):
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"a": {"status": "done"}}}))
    g1 = state_guard(tmp_path)
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"a": {"status": "PASS"}}}))
    g2 = state_guard(tmp_path)
    assert g1 and g2 and g1 != g2  # H0 守卫：state 变化后 guard 必变


# ---- configure_workflow_params（TS configureWorkflowParams/stampRunContext 移植）----


def test_configure_merges_params_and_preserves_phases(tmp_path: Path):
    write_state(
        tmp_path,
        WorkflowState.model_validate(
            {"params": {"run_mode": "full", "custom_existing": 1}, "phases": {"explore": {"status": "done"}}}
        ),
    )
    state = configure_workflow_params(
        tmp_path, {"run_mode": "api-only", "max_healing_attempts": 2}, "aa-execute"
    )
    assert state.params == {"run_mode": "api-only", "custom_existing": 1, "max_healing_attempts": 2}
    # phases 原样保留，且读回通过完整性校验
    back = read_state(tmp_path)
    assert back.phases.model_extra is not None
    assert back.phases.model_extra["explore"]["status"] == "done"


def test_configure_unknown_param_key_rejected_without_write(tmp_path: Path):
    write_state(tmp_path, WorkflowState.model_validate({"params": {"run_mode": "full"}}))
    before = (tmp_path / "workflow-state.json").read_text()
    with pytest.raises(AaError, match='unknown param "bogus"'):
        configure_workflow_params(tmp_path, {"bogus": 1}, "aa-workflow")
    assert (tmp_path / "workflow-state.json").read_text() == before


def test_configure_unknown_orchestrator_rejected(tmp_path: Path):
    with pytest.raises(AaError, match='unsupported orchestrator "aws-workflow"'):
        configure_workflow_params(tmp_path, {}, "aws-workflow")
    assert not (tmp_path / "workflow-state.json").exists()


@pytest.mark.parametrize(
    ("orchestrator", "interaction_mode", "active_scope"),
    [
        ("aa-intake", "interactive", "intake"),
        ("aa-execute", "autonomous", "execute"),
        ("aa-workflow", "autonomous", "full"),
    ],
)
def test_configure_stamps_run_context_per_orchestrator(
    tmp_path: Path, orchestrator: str, interaction_mode: str, active_scope: str
):
    configure_workflow_params(tmp_path, {}, orchestrator, stamped_at="2026-01-01T00:00:00+00:00")
    ctx = read_state(tmp_path).run_context
    assert ctx.orchestrator_skill == orchestrator
    assert ctx.interaction_mode == interaction_mode
    assert ctx.active_scope == active_scope
    assert ctx.stamped_at == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("orchestrator", "run_mode", "ok"),
    [
        ("aa-workflow", "full", True),
        ("aa-workflow", "api-only", True),
        ("aa-workflow", "review-case", True),
        ("aa-intake", "case-only", True),
        ("aa-intake", "api-only", False),
        ("aa-intake", "review-plan", False),
        ("aa-execute", "e2e-only", True),
        ("aa-execute", "case-only", False),
        ("aa-execute", "review-case", False),
    ],
)
def test_configure_run_mode_matrix(tmp_path: Path, orchestrator: str, run_mode: str, ok: bool):
    if ok:
        configure_workflow_params(tmp_path, {"run_mode": run_mode}, orchestrator)
        assert read_state(tmp_path).params["run_mode"] == run_mode
    else:
        with pytest.raises(AaError, match=f"{orchestrator} cannot run with run_mode {run_mode}"):
            configure_workflow_params(tmp_path, {"run_mode": run_mode}, orchestrator)


def test_configure_run_mode_check_reads_merged_params(tmp_path: Path):
    # 既存 run_mode=api-only 在 intake 下同样被拒绝（TS 校验合并后的 params）
    write_state(tmp_path, WorkflowState.model_validate({"params": {"run_mode": "api-only"}}))
    with pytest.raises(AaError, match="aa-intake cannot run with run_mode api-only"):
        configure_workflow_params(tmp_path, {}, "aa-intake")


def test_configure_idempotent_no_dirty_state(tmp_path: Path):
    stamped = "2026-01-01T00:00:00+00:00"
    configure_workflow_params(tmp_path, {"run_mode": "full"}, "aa-workflow", stamped_at=stamped)
    first = (tmp_path / "workflow-state.json").read_bytes()
    configure_workflow_params(tmp_path, {"run_mode": "full"}, "aa-workflow", stamped_at=stamped)
    second = (tmp_path / "workflow-state.json").read_bytes()
    assert first == second  # 重复 configure 不产生脏状态
    assert state_guard(tmp_path)  # 完整性哈希随写随新
    read_state(tmp_path)
