from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.phase6.conformance import (
    EXPECTED_RESIDUAL_MAPPINGS,
    PHASE5_ACCEPTANCE_PATH,
    PHASE5_HANDOFF_PATH,
    PHASE5_PROGRESS_PATH,
    ResidualDispositionV1,
    handoff_canonical_digest,
    parse_phase5_acceptance,
    parse_phase5_handoff,
)

ROOT = Path(__file__).resolve().parents[2]
CLOSEOUT_ROOT = ROOT / ".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout"


def _load_published(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_handoff(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _mutated_handoff(tmp_path: Path, mutate: Callable[[dict[str, object]], None]) -> Path:
    payload = _load_published(PHASE5_HANDOFF_PATH)
    mutate(payload)
    return _write_handoff(tmp_path / "phase6-handoff.json", payload)


def test_published_handoff_parses_without_fabricated_provider_admission() -> None:
    handoff = parse_phase5_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)

    assert handoff.source_commit == "e884bb88aadb9b3016f856c0c1a4b4ff3f351538"
    assert handoff.change_local_admission.admission_status == "accepted_with_waivers"
    assert handoff.provider_evidence.opencode.status == "incomplete"
    assert handoff.provider_evidence.opencode.session is None
    assert handoff.provider_evidence.opencode.publish_receipt is None
    assert handoff.provider_evidence.cursor.status == "deferred_out_of_scope"
    assert handoff.gate_evidence.local_disposition == "complete"
    assert handoff.gate_evidence.release_disposition == "blocked"
    assert handoff.compatibility_bridge_allowed is False


def test_parser_digest_is_stable_across_two_runs() -> None:
    first = parse_phase5_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)
    second = parse_phase5_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)

    assert handoff_canonical_digest(first) == handoff_canonical_digest(second)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_published_acceptance_is_truthful_and_does_not_pass_live_criteria() -> None:
    acceptance = parse_phase5_acceptance(PHASE5_ACCEPTANCE_PATH, repo_root=ROOT)

    assert tuple(item.criterion for item in acceptance.criteria) == tuple(range(1, 28))
    by_number = {item.criterion: item for item in acceptance.criteria}
    assert by_number[21].status != "passed"
    assert by_number[24].status == "locally_gated"
    assert by_number[25].status == "passed"
    assert by_number[27].status == "passed"
    assert all(item.status != "passed" or item.criterion not in {15, 21} for item in acceptance.criteria)


def test_parser_rejects_missing_provider_evidence(tmp_path: Path) -> None:
    def drop_provider(payload: dict[str, object]) -> None:
        payload.pop("provider_evidence")

    path = _mutated_handoff(tmp_path, drop_provider)

    with pytest.raises(ValueError, match="missing provider evidence"):
        parse_phase5_handoff(path, repo_root=ROOT)


def test_parser_rejects_fabricated_admitted_provider_evidence(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        provider = payload["provider_evidence"]
        assert isinstance(provider, dict)
        opencode = provider["opencode"]
        assert isinstance(opencode, dict)
        opencode["status"] = "achieved"
        opencode["session"] = "ses_fabricated"
        opencode["publish_receipt"] = "receipt_fabricated"

    path = _mutated_handoff(tmp_path, mutate)

    with pytest.raises(ValueError, match="provider evidence that does not exist"):
        parse_phase5_handoff(path, repo_root=ROOT)


def test_parser_rejects_unresolved_gate_claimed_complete(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        gate = payload["gate_evidence"]
        assert isinstance(gate, dict)
        gate["release_disposition"] = "complete"
        gate["unresolved"] = []

    path = _mutated_handoff(tmp_path, mutate)

    with pytest.raises(ValueError, match="unresolved gate"):
        parse_phase5_handoff(path, repo_root=ROOT)


def test_parser_rejects_mutable_path(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        admission = payload["change_local_admission"]
        assert isinstance(admission, dict)
        admission["path"] = "/tmp/admission.json"

    path = _mutated_handoff(tmp_path, mutate)

    with pytest.raises(ValueError, match="mutable path"):
        parse_phase5_handoff(path, repo_root=ROOT)


def test_parser_rejects_secret_bearing_payload(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        provider = payload["provider_evidence"]
        assert isinstance(provider, dict)
        opencode = provider["opencode"]
        assert isinstance(opencode, dict)
        opencode["api_key"] = "sk-test-secret"

    path = _mutated_handoff(tmp_path, mutate)

    with pytest.raises(ValueError, match="secret-bearing"):
        parse_phase5_handoff(path, repo_root=ROOT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("tree_id", "tree-1"),
        lambda payload: payload.__setitem__("head_tree_id", "0" * 64),
        lambda payload: payload.__setitem__("HEAD", {"pointer": "workspace/HEAD.json"}),
        lambda payload: payload.__setitem__("whole_tree_export_digest", "0" * 64),
        lambda payload: payload.__setitem__("result_tree", {"digest": "0" * 64}),
    ],
)
def test_parser_rejects_old_result_tree_field(
    tmp_path: Path, mutation: Callable[[dict[str, object]], None]
) -> None:
    path = _mutated_handoff(tmp_path, mutation)

    with pytest.raises(ValueError, match="old result-tree field"):
        parse_phase5_handoff(path, repo_root=ROOT)


def test_residual_ledger_advances_task26_and_task27_without_closing_task24() -> None:
    raw = json.loads((CLOSEOUT_ROOT / "residual-disposition.json").read_text(encoding="utf-8"))
    records = tuple(ResidualDispositionV1.model_validate(item) for item in raw)
    actual = {
        (record.source_plan, record.source_task): (record.disposition, record.replacement_task)
        for record in records
    }

    assert actual == EXPECTED_RESIDUAL_MAPPINGS
    assert actual[("phase-5", "Task 24")] == ("carried_forward", 3)
    assert actual[("phase-3", "OpenCode live")] == ("carried_forward", 3)
    assert actual[("phase-5", "Task 25")] == ("deferred_out_of_scope", None)
    assert actual[("phase-5", "Task 26")] == ("verified_complete", None)
    assert actual[("phase-5", "Task 27")] == ("verified_complete", None)


def test_phase5_progress_does_not_mark_task24_complete() -> None:
    text = PHASE5_PROGRESS_PATH.read_text(encoding="utf-8")

    assert "Task 24: closeout — remains incomplete" in text
    assert "Task 24: complete" not in text
    assert "Task 25: deferred_out_of_scope" in text
    assert "Task 26: locally gated/complete" in text
    assert "Task 27: complete" in text
