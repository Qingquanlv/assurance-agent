from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config
from tests.unit.retro.archive_fixtures import make_archived_change
from tests.unit.retro.proposal_fixtures import memory_proposal_dict

from assurance_agent.exceptions import AaError
from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.collect_stage import run_retro_collect
from assurance_agent.retro.types import RetroProposal


def _setup_retro(tmp_path: Path, retro_id: str = "retro-test") -> Path:
    """Create a retro directory with a valid context.json."""
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)
    run_retro_collect(tmp_path, retro_id=retro_id, is_terminal=lambda _d, _c: True)
    return tmp_path / "qa" / "retro" / retro_id


def _write_proposals(retro_dir: Path, proposals: list[dict] | None = None) -> None:
    retro_dir.mkdir(parents=True, exist_ok=True)
    if proposals is None:
        proposals = [
            memory_proposal_dict(id="P-1", payload={"body": "append this"}, evidence_ids=["CH-1#F-1"]),
        ]
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": proposals}), encoding="utf-8")
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")


def test_run_retro_accept_returns_proposals(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    _write_proposals(retro_dir)
    proposals = run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)
    assert isinstance(proposals, list)
    assert len(proposals) > 0
    assert all(isinstance(p, RetroProposal) for p in proposals)


def test_run_retro_accept_writes_review_queue(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    _write_proposals(retro_dir)
    run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)
    assert (retro_dir / "review-queue.md").is_file()


def test_run_retro_accept_calls_complete_retro_stage(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    _write_proposals(retro_dir)
    run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)
    state = json.loads((tmp_path / "qa" / "retro" / "_state.json").read_text())
    assert state["last_retro_id"] == "retro-test"
    for record in state["consumed_changes"].values():
        if record.get("retro_id") == "retro-test":
            assert record["terminal"] is True


def test_run_retro_accept_no_proposals_returns_empty(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": []}), encoding="utf-8")
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")
    proposals = run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)
    assert proposals == []
    # complete_retro_stage should still have been called
    assert (retro_dir / "review-queue.md").is_file()


def test_run_retro_accept_missing_proposals_raises(tmp_path: Path) -> None:
    _setup_retro(tmp_path)
    # do NOT write proposals.json
    with pytest.raises(AaError):
        run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)


def test_run_retro_accept_rewrites_legacy_shape(tmp_path: Path) -> None:
    """Accept stage must persist canonical finding_kind/payload shape."""
    retro_dir = _setup_retro(tmp_path)
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "proposals.json").write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "id": "P-1",
                        "apply_kind": "memory_append",
                        "target": ".aa/memory/aa-run.md",
                        "problem": "flaky fixture use",
                        "proposed_change": "remember to check fixtures",
                        "evidence_ids": ["CH-1#F-1"],
                        "eval_suite": "workflow-run",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")
    proposals = run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)
    assert len(proposals) == 1
    rewritten = json.loads((retro_dir / "proposals.json").read_text())
    entry = rewritten["proposals"][0]
    assert entry["finding_kind"] == "prompt_rule"
    assert entry["payload"]["body"] == "remember to check fixtures"


def test_run_retro_accept_rejects_unknown_evidence_without_completing(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    _write_proposals(
        retro_dir,
        [
            memory_proposal_dict(
                id="P-UNKNOWN",
                payload={"body": "append this"},
                evidence_ids=["CH-1#UNKNOWN"],
            )
        ],
    )

    with pytest.raises(AaError, match="evidence_ids absent from context"):
        run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)

    state = json.loads((tmp_path / "qa" / "retro" / "_state.json").read_text())
    assert state["consumed_changes"]["CH-1"]["terminal"] is False
    assert not (retro_dir / "review-queue.md").exists()


def test_run_retro_accept_rejects_duplicate_ids_without_completing(tmp_path: Path) -> None:
    retro_dir = _setup_retro(tmp_path)
    _write_proposals(
        retro_dir,
        [
            memory_proposal_dict(id="P-DUP", payload={"body": "first"}),
            memory_proposal_dict(id="P-DUP", payload={"body": "second"}),
        ],
    )

    with pytest.raises(AaError, match="duplicate proposal id: P-DUP"):
        run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)

    state = json.loads((tmp_path / "qa" / "retro" / "_state.json").read_text())
    assert state["consumed_changes"]["CH-1"]["terminal"] is False
    assert not (retro_dir / "review-queue.md").exists()
