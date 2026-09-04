from __future__ import annotations

import pytest

from tests.product.checkpoint_r_support import installed_live_agent_rows

_REQUIRED_EVIDENCE = (
    "candidate_sha",
    "source_digests",
    "wheel_digests",
    "opencode_version",
    "opencode_binary_digest",
    "provider",
    "model",
    "runtime_binding_digest",
    "contract_digest",
    "result_schema_digest",
    "finalizer_digest",
    "product_lock_digest",
    "graph_revision",
    "policy_digest",
    "resource_digest",
    "secret_handle_digest",
    "attempt_key_digest",
    "source_terminal_receipt_digest",
    "promotion_receipt_digest",
    "effect_receipts",
    "resources_released",
    "ci_run_identity",
    "prompt_count",
    "local_result_validated",
    "phase_write_claims",
    "no_direct_project_write",
    "host_receipt_reused",
)


def test_checkpoint_r_live_module_keeps_credential_free_inventory() -> None:
    rows = installed_live_agent_rows()
    assert len(rows) == 33
    assert tuple(row.contract_id for row in rows) == tuple(sorted({row.contract_id for row in rows}))


@pytest.mark.checkpoint_r_live
@pytest.mark.parametrize("row", installed_live_agent_rows(), ids=lambda row: row.contract_id)
def test_live_agent_contract_through_product_ports(row, protected_candidate) -> None:
    result = protected_candidate.execute_agent_attempt(row)
    assert result.receipt.attempt_key_digest == row.attempt_key_digest
    assert result.receipt.contract_digest == row.contract_digest
    assert result.receipt.source_terminal_receipt_digest
    assert result.evidence["prompt_count"] == 1
    assert result.evidence["status"] == "passed"
    assert result.evidence["local_result_validated"] is True
    assert result.evidence["no_direct_project_write"] is True
    assert result.evidence["host_receipt_reused"] is True
    assert result.evidence["resources_released"] is True
    assert result.evidence["phase_write_claims"]
    assert result.evidence["result_schema_digest"] != result.evidence["contract_digest"]
    for key in _REQUIRED_EVIDENCE:
        assert result.evidence[key] not in (None, ""), key
