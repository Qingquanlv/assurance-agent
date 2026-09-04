from __future__ import annotations

import pytest

from tests.product.checkpoint_r_support import installed_live_agent_rows


@pytest.mark.checkpoint_r_live
@pytest.mark.parametrize("row", installed_live_agent_rows(), ids=lambda row: row.contract_id)
def test_live_agent_contract_through_product_ports(row, protected_candidate) -> None:
    result = protected_candidate.execute_agent_attempt(row)
    assert result.receipt.attempt_key_digest == row.attempt_key_digest
    assert result.receipt.contract_digest == row.contract_digest
    assert result.receipt.source_terminal_receipt_digest
    assert result.evidence["prompt_count"] == 1
    assert result.evidence["status"] == "passed"
