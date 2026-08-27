from __future__ import annotations

from agent_runtime_contracts.schema import MAX_DIAGNOSTIC_COUNT, MAX_DIAGNOSTIC_LENGTH

from tests.phase4.security_canaries import (
    assert_canaries_absent,
    inject_unique_canaries,
    redacted_diagnostics_are_bounded,
)


def test_secret_canaries_never_persist_in_lock_ledger_or_diagnostics() -> None:
    scan = inject_unique_canaries()
    assert_canaries_absent(scan)
    assert redacted_diagnostics_are_bounded(scan)
    assert len(scan.diagnostics) <= MAX_DIAGNOSTIC_COUNT
    assert all(len(item) <= MAX_DIAGNOSTIC_LENGTH for item in scan.diagnostics)
