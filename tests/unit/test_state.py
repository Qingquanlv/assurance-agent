from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import PhaseState, WorkflowState
from assurance_agent.workflow.core.state import (
    StateIntegrityError,
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
    f = tmp_path / "workflow-state.yaml"
    doc = yaml.safe_load(f.read_text())
    doc["phases"]["a"]["status"] = "FORGED"  # 篡改但不更新哈希
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(StateIntegrityError):
        read_state(tmp_path)


def test_legacy_without_hash_tolerated(tmp_path: Path):
    f = tmp_path / "workflow-state.yaml"
    f.write_text(yaml.safe_dump({"phases": {"a": {"status": "done"}}}))  # 无 _integrity
    got = read_state(tmp_path)
    assert got.phases.model_extra is not None
    assert got.phases.model_extra["a"]["status"] == "done"


def test_state_guard_changes_with_content(tmp_path: Path):
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"a": {"status": "done"}}}))
    g1 = state_guard(tmp_path)
    write_state(tmp_path, WorkflowState.model_validate({"phases": {"a": {"status": "PASS"}}}))
    g2 = state_guard(tmp_path)
    assert g1 and g2 and g1 != g2  # H0 守卫：state 变化后 guard 必变
