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
    for name, handle in handles.items():
        assert handle.ledger_key in produced, f"{name} reads an undeclared write: {handle.ledger_key}"
