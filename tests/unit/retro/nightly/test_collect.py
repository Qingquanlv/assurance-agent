from __future__ import annotations

import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.retro.nightly.driver import _default_is_terminal, collect_nightly
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE,
    NIGHTLY_NOOP,
    NIGHTLY_OK,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from tests.unit.retro.archive_fixtures import make_archived_change
from tests.unit.retro.proposal_fixtures import memory_proposal_dict


def _opts(sut: Path, *, dry_run: bool = False) -> NightlyOptions:
    return NightlyOptions(
        sut=str(sut),
        retro_id="retro-test",
        dry_run=dry_run,
        agent="fake-agent",
        min_evidence=1,
        rework_alert=3,
        skip_eval=False,
        last=10,
    )


def _write_proposals(sut: Path, retro_id: str) -> None:
    retro_dir = sut / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "proposals.json").write_text(
        json.dumps(
            {
                "proposals": [
                    memory_proposal_dict(
                        id="P-1",
                        payload={"body": "append this"},
                        proposed_change="append this",
                        eval_suite="s",
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")


def test_collect_success_exit_0(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        _write_proposals(sut, "retro-test")
        return 0

    code = collect_nightly(_opts(sut), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_OK
    retro_dir = sut / "qa" / "retro" / "retro-test"
    assert (retro_dir / "context.json").exists()
    assert (retro_dir / "review-queue.md").exists()
    ctx = json.loads((retro_dir / "context.json").read_text())
    assert ctx["signal_count"] > 0
    assert ctx["window"]["change_count"] == 1


def test_collect_no_changes_exit_10(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "archive").mkdir(parents=True)

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        raise AssertionError("agent must not run when there are no candidates")

    code = collect_nightly(_opts(tmp_path), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_NOOP


def test_collect_zero_signals_exit_10(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[], gate_pushbacks=0, apply_status="none")
    change_root = sut / "qa" / "archive" / "CH-1"
    (change_root / "healing" / "api-apply-summary.json").unlink()
    (change_root / "events.jsonl").write_text(
        '{"type": "workflow_started", "change_id": "CH-1"}\n', encoding="utf-8"
    )

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        raise AssertionError("agent must not run on zero-signal no-op")

    code = collect_nightly(_opts(sut), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_NOOP


def test_collect_agent_failure_exit_40(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}])

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        return 7

    code = collect_nightly(_opts(sut), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_FAILURE


def test_collect_rewrites_legacy_proposals_to_canonical_shape(tmp_path: Path) -> None:
    """Accept gate must persist finding_kind/payload after the agent writes prose-only."""
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)

    def agent_runner(cmd: str, retro_dir: Path) -> int:
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
        return 0

    code = collect_nightly(_opts(sut), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_OK
    rewritten = json.loads(
        (sut / "qa" / "retro" / "retro-test" / "proposals.json").read_text(encoding="utf-8")
    )
    entry = rewritten["proposals"][0]
    assert entry["finding_kind"] == "prompt_rule"
    assert entry["payload"]["body"] == "remember to check fixtures"


def test_collect_unroutable_proposals_exit_40(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}])

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        retro_dir.mkdir(parents=True, exist_ok=True)
        (retro_dir / "proposals.json").write_text(
            json.dumps(
                {
                    "proposals": [
                        {"id": "P-BAD", "apply_kind": "contract_field", "problem": "p"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")
        return 0

    code = collect_nightly(_opts(sut), agent_runner=agent_runner, is_terminal=lambda root, cid: True)
    assert code == NIGHTLY_FAILURE


def test_default_is_terminal_treats_archived_change_as_terminal(tmp_path: Path) -> None:
    """Regression: archived changes must be terminal by construction.

    Re-projecting GraphRuntime status against an archived directory is
    unreliable (archive omits some case artifacts). The archive gate only
    lets a change through after execution/healing/review already passed.
    """
    write_aa_config(tmp_path)
    change_dir = make_archived_change(tmp_path, "CH-ARCHIVED", failures=[])

    assert _default_is_terminal(tmp_path, change_dir, "CH-ARCHIVED") is True


def test_collect_dry_run_stops_before_agent_exit_0(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path
    make_archived_change(sut, "CH-1", failures=[{"classification": "assertion"}])

    def agent_runner(cmd: str, retro_dir: Path) -> int:
        raise AssertionError("dry-run must not invoke agent")

    code = collect_nightly(
        _opts(sut, dry_run=True), agent_runner=agent_runner, is_terminal=lambda root, cid: True
    )
    assert code == NIGHTLY_OK
    assert (sut / "qa" / "retro" / "retro-test" / "context.json").exists()
