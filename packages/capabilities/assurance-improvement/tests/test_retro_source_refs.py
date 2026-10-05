"""Retro source collection dedupes by path and keeps the handwritten conflict rule."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.retro_identity import (
    collect_retro_source_refs,
    prepare_retro_identity,
)
from assurance_improvement.graphs.retro import RetroFlowInput
from assurance_intake.contracts import EvidenceArtifactRefV1


def _ref(path: str, digest: str) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=digest)


def test_duplicate_paths_collapse_before_the_retro_id_is_derived() -> None:
    ref = _ref("qa/explore/exploration.json", "a" * 64)
    collected = collect_retro_source_refs(source_refs=(ref, ref))
    identity = prepare_retro_identity(
        change_id="CH-A",
        window=None,
        source_refs=collected,
        report_receipt_digest="b" * 64,
    )
    entered = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "source_refs": [ref, ref],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "b" * 64},
        }
    )
    assert entered.source_refs == (ref,)
    assert entered.retro_id == identity.retro_id


def test_an_explicit_retro_id_is_kept_while_paths_are_deduped() -> None:
    ref = _ref("qa/a.json", "a" * 64)
    entered = RetroFlowInput.model_validate(
        {"change_id": "CH-A", "retro_id": "retro-kept", "source_refs": [ref, ref]}
    )
    assert entered.retro_id == "retro-kept"
    assert entered.source_refs == (ref,)


def test_preparation_and_reviewed_refs_replace_and_other_conflicts_fail() -> None:
    stale = _ref("qa/explore/exploration.json", "a" * 64)
    current = _ref("qa/explore/exploration.json", "b" * 64)
    replaced = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "retro_id": "retro-kept",
            "source_refs": [stale],
            "preparation_refs": [current],
        }
    )
    assert replaced.source_refs == (current,)
    reviewed = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "retro_id": "retro-kept",
            "source_refs": [stale],
            "reviewed_refs": [current],
        }
    )
    assert reviewed.source_refs == (current,)
    with pytest.raises(ValidationError, match="conflicting Retro source digest"):
        RetroFlowInput.model_validate({"change_id": "CH-A", "source_refs": [stale], "report_refs": [current]})


def test_omitted_retro_id_binds_the_current_change_and_report_receipt() -> None:
    ref = _ref("qa/results/report/report.md", "a" * 64)
    current = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "source_refs": [ref],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "b" * 64},
        }
    )
    other_change = RetroFlowInput.model_validate(
        {
            "change_id": "CH-B",
            "source_refs": [ref],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "b" * 64},
        }
    )
    other_receipt = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "source_refs": [ref],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "c" * 64},
        }
    )
    assert current.retro_id != other_change.retro_id
    assert current.retro_id != other_receipt.retro_id
    assert current.window is not None
    assert current.window.change_ids == ("CH-A",)


def test_retro_source_refs_include_the_runtime_snapshot_and_history() -> None:
    runtime = _ref("qa/results/workflow/" + "c" * 64 + "/pre-retro/workflow-evidence.json", "d" * 64)
    history = _ref("qa/cases/reviews/epochs/0/rounds/0.json", "e" * 64)
    later = _ref("qa/cases/reviews/epochs/0/rounds/1.json", "f" * 64)
    entered = RetroFlowInput.model_validate(
        {
            "change_id": "CH-A",
            "retro_id": "retro-kept",
            "runtime_ref": runtime,
            "history_refs": [later, history],
        }
    )
    assert runtime in entered.source_refs
    assert history in entered.source_refs
    assert later in entered.source_refs


def test_explicit_window_is_preserved() -> None:
    from assurance_improvement.contracts.retro import RetroSelectionSnapshot, RetroWindow

    window = RetroWindow(
        selection=RetroSelectionSnapshot(
            mode="change_ids",
            requested_change_ids=("CH-A", "CH-B"),
        ),
        change_ids=("CH-A", "CH-B"),
    )
    entered = RetroFlowInput.model_validate(
        {"change_id": "CH-A", "window": window.model_dump(mode="json"), "source_refs": []}
    )
    assert entered.window is not None
    assert entered.window.model_dump(mode="json") == window.model_dump(mode="json")
