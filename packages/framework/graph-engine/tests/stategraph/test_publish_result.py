from __future__ import annotations

import pytest

from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.stategraph.ledger import NamedWrite, ledger_refs
from graph_engine.stategraph.publish import bind_produced_artifacts, publish_outcome, publish_result

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


def test_publish_outcome_copies_a_nonempty_string() -> None:
    publish = publish_outcome("decision")
    assert publish({}, {"decision": "pass"}, None) == {"outcome": "pass"}
    assert publish({}, {"decision": "pass"}, None, committed=())["outcome"] == "pass"


@pytest.mark.parametrize("output", [{}, {"decision": ""}, {"decision": None}, {"decision": 1}])
def test_publish_outcome_rejects_a_missing_or_blank_value(output: dict[str, object]) -> None:
    publish = publish_outcome("decision", channel="status")
    with pytest.raises(TypeError, match="output\\['decision'\\] must be a nonempty str"):
        publish({}, output, None)


def test_publish_outcome_lets_the_other_publish_win_except_the_channel() -> None:
    def other(state: dict[str, object], output: object, receipt: object) -> dict[str, object]:
        del state, output, receipt
        return {"outcome": "from-other", "status": "kept"}

    publish = publish_outcome("decision", then=other)
    update = publish({}, {"decision": "pass"}, None)
    assert update == {"outcome": "pass", "status": "kept"}


def test_publish_outcome_forwards_committed_only_when_accepted() -> None:
    seen: dict[str, object] = {}

    def accepts(
        state: dict[str, object],
        output: object,
        receipt: object,
        *,
        committed: object = (),
    ) -> dict[str, object]:
        del state, output, receipt
        seen["committed"] = committed
        return {"status": "kept"}

    def rejects(state: dict[str, object], output: object, receipt: object) -> dict[str, object]:
        del state, output, receipt
        return {"status": "kept"}

    ref = ArtifactRef(path="qa/note.md", digest="a" * 64)
    forwarded = publish_outcome("decision", then=accepts)
    forwarded({}, {"decision": "pass"}, None, committed=(ref,))
    assert seen["committed"] == (ref,)
    plain = publish_outcome("decision", then=rejects)
    assert plain({}, {"decision": "pass"}, None, committed=(ref,)) == {"outcome": "pass", "status": "kept"}


def test_publish_outcome_composes_under_bind_produced_artifacts() -> None:
    def other(state: dict[str, object], output: object, receipt: object) -> dict[str, object]:
        del state, output, receipt
        return {
            "status": "kept",
            "outcome": "from-other",
            "artifact_ledger": {"intake.case": [{"path": "qa/other.yaml", "digest": "e" * 64}]},
        }

    bound = bind_produced_artifacts(
        publish_outcome("decision", then=other),
        namespace="intake",
        writes=(NamedWrite(name="note", root="qa/note.md"),),
    )
    ref = ArtifactRef(path="qa/note.md", digest="a" * 64)
    update = bound({}, {"decision": "pass"}, None, committed=(ref,))
    assert update["outcome"] == "pass"
    assert update["status"] == "kept"
    assert ledger_refs(update["artifact_ledger"], "intake.note") == [
        {"path": "qa/note.md", "digest": "a" * 64}
    ]
