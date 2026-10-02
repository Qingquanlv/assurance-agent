from __future__ import annotations

import pytest

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.stategraph.publish import publish_result

_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)


def test_publish_result_stores_output_and_receipt_when_identity_matches() -> None:
    publish = publish_result(
        "reviewed_case", receipt="case_receipt", identity=("change_id", "coverage_epoch")
    )
    update = publish(
        {"change_id": "CH-1", "coverage_epoch": 0},
        {"change_id": "CH-1", "coverage_epoch": 0, "status": "pass"},
        _RECEIPT,
    )
    assert update["reviewed_case"] == {"change_id": "CH-1", "coverage_epoch": 0, "status": "pass"}
    assert update["case_receipt"] == _RECEIPT.model_dump(mode="json")


def test_publish_result_rejects_identity_drift() -> None:
    publish = publish_result("plan", identity=("change_id",))
    with pytest.raises(ValueError, match="change_id"):
        publish({"change_id": "CH-1"}, {"change_id": "CH-2"}, None)


def test_publish_result_skips_identity_fields_the_state_does_not_have() -> None:
    publish = publish_result("plan", identity=("change_id",), receipt="receipt")
    update = publish({}, {"change_id": "CH-1"}, None)
    assert update["plan"] == {"change_id": "CH-1"}
    assert update["receipt"] is None
