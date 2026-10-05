from __future__ import annotations

from agent_runtime_contracts.ops import ArtifactHandle

from assurance_intake import handoff
from assurance_intake.ops import router


def _produced_ledger_keys() -> dict[str, bool]:
    keys: dict[str, bool] = {}
    for op in router.ops().values():
        for write in op.ledger_writes():
            keys[op.artifact(write.name).ledger_key] = write.many
    return keys


def test_shared_handles_name_a_write_some_intake_op_declares() -> None:
    produced = _produced_ledger_keys()
    handles = {name: value for name, value in vars(handoff).items() if isinstance(value, ArtifactHandle)}
    assert handles
    # These handles authenticate input refs or plan-bound source documents;
    # they do not name a single Intake output declaration.
    input_sources = {
        "PREPARATION",
        "CASE_INVENTORY",
        "CASE_EXPLORATION",
        "CASE_UI",
        "CASE_API",
        "CASE_CATALOG",
        "CASE_KNOWLEDGE",
    }
    for name, handle in handles.items():
        if name in input_sources:
            assert handle.slot is not None or handle.ref is not None
            continue
        assert handle.ledger_key in produced, f"{name} reads an undeclared write: {handle.ledger_key}"
    reviewed = handles["REVIEWED_CASE"]
    assert reviewed.ledger_key == "intake.reviewed_case"
    assert reviewed.loader is handoff.load_reviewed_case
    assert produced["intake.reviewed_case"] is False
    from assurance_intake.ops.coverage_rework import op as coverage_rework

    rework = handles["REWORK_CONTEXT"]
    assert rework.ledger_key == coverage_rework.artifact("rework").ledger_key
    assert rework.optional is True
    assert rework.loader is handoff.load_case_rework
    assert rework.check is handoff.check_case_rework
