from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

from assurance_agent.cli import main
from tests.unit.retro.archive_fixtures import make_archived_change


def test_retro_json_stdout_shape(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)
        result = runner.invoke(main, ["retro", "--retro-id", "retro-x", "--change", "CH-1", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["retro_id"] == "retro-x"
        assert payload["change_count"] == 1
        assert payload["signal_count"] >= 1
        assert (root / "qa" / "retro" / "retro-x" / "context.json").exists()


def test_retro_since_and_change_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["retro", "--since", "2026-01-01", "--change", "CH-1"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_retro_immutable_when_promotions_present(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[])
        retro_dir = root / "qa" / "retro" / "retro-locked"
        retro_dir.mkdir(parents=True)
        (retro_dir / "promotions.json").write_text("[]", encoding="utf-8")
        result = runner.invoke(main, ["retro", "--retro-id", "retro-locked", "--change", "CH-1"])
        assert result.exit_code != 0
        assert "immutable" in result.output


def test_retro_nightly_collect_success_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)
        agent = root / "fake-agent.sh"
        retro_glob = "qa/retro"
        agent.write_text(
            "#!/usr/bin/env bash\n"
            f'd="$(ls -1d {retro_glob}/retro-* | tail -1)"\n'
            'printf \'{"proposals":[{"id":"P-1","apply_kind":"memory_append",'
            '"body":"x","eval_suite":"s","evidence_ids":["CH-1#F-1"]}]}\' > "$d/proposals.json"\n'
            'printf "# summary\\n" > "$d/retro-summary.md"\n',
            encoding="utf-8",
        )
        os.chmod(agent, 0o755)
        result = runner.invoke(
            main,
            [
                "retro",
                "nightly",
                "collect",
                "--sut",
                str(root),
                "--retro-id",
                "retro-n",
                "--agent",
                f"bash {agent}",
                "--min-evidence",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (root / "qa" / "retro" / "retro-n" / "review-queue.md").exists()


def test_retro_nightly_collect_noop_exit_10(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        write_aa_config(root)
        (root / "qa" / "archive").mkdir(parents=True)
        result = runner.invoke(
            main, ["retro", "nightly", "collect", "--sut", str(root), "--retro-id", "retro-empty"]
        )
        assert result.exit_code == 10


def test_retro_nightly_collect_dry_run_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}])
        result = runner.invoke(
            main, ["retro", "nightly", "collect", "--sut", str(root), "--retro-id", "retro-dry", "--dry-run"]
        )
        assert result.exit_code == 0
        assert (root / "qa" / "retro" / "retro-dry" / "context.json").exists()


# --- retro promotion loop: promote / complete / apply / rollback (spec §6) ---

_PROPOSAL = {
    "id": "P-1",
    "apply_kind": "memory_append",
    "eval_suite": "workflow-run",
    "target": ".aa/memory/aa-run.md",
    "problem": "flaky fixture use",
    "proposed_change": "remember to check fixtures",
    "evidence_ids": ["CH-1#F-1"],
}


def _seed_retro_dir(root: Path, retro_id: str, proposals: list[dict] | None = None) -> Path:
    retro_dir = root / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "proposals.json").write_text(
        json.dumps({"proposals": proposals if proposals is not None else [dict(_PROPOSAL)]}),
        encoding="utf-8",
    )
    context = {
        "retro_id": retro_id,
        "generated_at": "2026-07-17T00:00:00Z",
        "window": {"since": None, "change_count": 1, "change_ids": ["CH-1"], "change_sources": []},
        "signals": {
            "failure_distribution": [
                {
                    "category": "assertion",
                    "count": 1,
                    "changes": ["CH-1"],
                    "top_modules": [],
                    "evidence_ids": ["CH-1#F-1"],
                }
            ],
            "gate_pushback": [],
            "healing_efficiency": {"attempts": 0, "applied": 0, "success_rate": 0.0, "evidence_ids": []},
            "human_decisions": [],
            "reclassifications": [],
            "skill_execution": [],
            "eval_trend": [],
        },
        "signal_count": 1,
    }
    (retro_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
    return retro_dir


def _seed_suite(root: Path, suite: str = "workflow-run") -> None:
    suites = root / "eval" / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    (suites / f"{suite}.yaml").write_text(
        f"name: {suite}\nscorer: workflow_run\n"
        "thresholds:\n"
        "  - metric: evidence_integrity\n"
        "    gate: hard\n"
        "    op: gte\n"
        "    value: 0.95\n",
        encoding="utf-8",
    )


def _seed_baseline(root: Path, metric: float = 1.0) -> None:
    baselines = root / "eval" / "baselines"
    baselines.mkdir(parents=True, exist_ok=True)
    (baselines / "main.json").write_text(
        json.dumps(
            {
                "workflow-run": {
                    "run_id": "base",
                    "approved_at": "2026-07-16T00:00:00Z",
                    "approved_by": "test",
                    "metrics": {"evidence_integrity": metric},
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_eval_factory(calls: list):  # noqa: ANN202
    def factory(data_root, sut_root):  # noqa: ANN001, ANN202
        def runner(*, suite, sut_dir=None, engine_root=None, extra_memory_dir=None):  # noqa: ANN001
            calls.append(suite)
            run_id = f"eval-promote-{len(calls)}"
            run = Path(sut_dir) / "eval" / "out" / "runs" / run_id
            run.mkdir(parents=True, exist_ok=True)
            (run / "metrics.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "suite": suite,
                        "sample_count": 1,
                        "metrics": {"evidence_integrity": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            return {"run_id": run_id, "verdict": "pass", "metrics": {"evidence_integrity": 1.0}}

        return runner

    return factory


def _read_promotions(retro_dir: Path) -> dict:
    return json.loads((retro_dir / "promotions.json").read_text(encoding="utf-8"))


def test_retro_promote_records_needs_rework_event() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-p")
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-1",
                "--decision",
                "needs_rework",
                "--decided-by",
                "LQ",
                "--rework-note",
                "thin evidence",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload == {
            "retro_id": "retro-p",
            "proposal_id": "P-1",
            "decision": "needs_rework",
            "state": "needs_rework",
            "idempotent": False,
        }
        raw = _read_promotions(retro_dir)
        assert raw["schema_version"] == "2"
        assert len(raw["events"]) == 1
        event = raw["events"][0]
        assert event["type"] == "review_decision"
        assert event["decision"] == "needs_rework"
        assert event["actor"] == "LQ"
        assert event["rework_note"] == "thin evidence"
        assert event["at"]


def test_retro_promote_unknown_proposal() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        _seed_retro_dir(Path(fs), "retro-p")
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-9",
                "--decision",
                "rejected",
                "--decided-by",
                "LQ",
            ],
        )
        assert result.exit_code == 1
        assert "proposal not found" in result.output


def test_retro_promote_rejects_non_memory_append() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        proposal = dict(_PROPOSAL, apply_kind="skill_edit")
        retro_dir = _seed_retro_dir(root, "retro-p", [proposal])
        _seed_suite(root)
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-1",
                "--decision",
                "promoted",
                "--decided-by",
                "LQ",
            ],
        )
        assert result.exit_code == 1
        assert "memory_append" in result.output
        assert not (retro_dir / "promotions.json").exists()


def test_retro_promote_rejects_unknown_suite() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-p")
        _seed_suite(root, "other-suite")
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-1",
                "--decision",
                "promoted",
                "--decided-by",
                "LQ",
            ],
        )
        assert result.exit_code == 1
        assert "suite not found" in result.output
        assert not (retro_dir / "promotions.json").exists()


def test_retro_promote_rejects_unsafe_target() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        proposal = dict(_PROPOSAL, target="../escape.md")
        retro_dir = _seed_retro_dir(root, "retro-p", [proposal])
        _seed_suite(root)
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-1",
                "--decision",
                "promoted",
                "--decided-by",
                "LQ",
            ],
        )
        assert result.exit_code == 1
        assert "outside .aa/memory" in result.output
        assert not (retro_dir / "promotions.json").exists()


def test_retro_promote_promoted_triggers_resume_and_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-p")
        _seed_suite(root)
        _seed_baseline(root)
        calls: list = []
        monkeypatch.setattr(
            "assurance_agent.commands.retro_cmd._build_eval_runner", _fake_eval_factory(calls)
        )
        result = runner.invoke(
            main,
            [
                "retro",
                "promote",
                "--retro",
                "retro-p",
                "--proposal",
                "P-1",
                "--decision",
                "promoted",
                "--decided-by",
                "LQ",
            ],
        )
        assert result.exit_code == 0, result.output
        # promote wrote the review_decision event, then immediately ran the gate
        assert calls == ["workflow-run"]
        events = _read_promotions(retro_dir)["events"]
        kinds = [(e["type"], e.get("decision") or e.get("result")) for e in events]
        assert ("review_decision", "promoted") in kinds
        assert ("eval_completed", "pass") in kinds
        assert ("application", "applied") in kinds
        # Landing uses the marker-idempotent apply path on the proposal target.
        memory = root / ".aa" / "memory" / "aa-run.md"
        assert memory.exists()
        assert "retro:retro-p#P-1" in memory.read_text(encoding="utf-8")


def test_retro_promote_terminal_idempotent_no_reeval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-p")
        _seed_suite(root)
        _seed_baseline(root)
        calls: list = []
        monkeypatch.setattr(
            "assurance_agent.commands.retro_cmd._build_eval_runner", _fake_eval_factory(calls)
        )
        args = [
            "retro",
            "promote",
            "--retro",
            "retro-p",
            "--proposal",
            "P-1",
            "--decision",
            "promoted",
            "--decided-by",
            "LQ",
        ]
        first = runner.invoke(main, args)
        assert first.exit_code == 0, first.output
        event_count = len(_read_promotions(retro_dir)["events"])

        second = runner.invoke(main, args)
        assert second.exit_code == 0, second.output
        payload = json.loads(second.output.strip().splitlines()[-1])
        assert payload["idempotent"] is True
        assert payload["decision"] == "promoted"
        assert payload["state"] == "applied"
        # no duplicate event, no repeated eval
        assert len(_read_promotions(retro_dir)["events"]) == event_count
        assert calls == ["workflow-run"]


def test_retro_complete_marks_consumed_terminal() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_root = root / "qa" / "retro"
        retro_root.mkdir(parents=True)
        (retro_root / "_state.json").write_text(
            json.dumps(
                {
                    "last_retro_id": None,
                    "consumed_changes": {
                        "CH-1": {
                            "source": "archive",
                            "consumed_at": "2026-07-17T00:00:00Z",
                            "retro_id": "retro-c",
                            "terminal": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["retro", "complete", "--retro", "retro-c"])
        assert result.exit_code == 0, result.output
        state = json.loads((retro_root / "_state.json").read_text(encoding="utf-8"))
        assert state["last_retro_id"] == "retro-c"
        assert state["consumed_changes"]["CH-1"]["terminal"] is True


def test_retro_apply_live_marker_idempotent_legacy_promotions() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-a")
        # legacy list-format promotions.json must still be readable
        (retro_dir / "promotions.json").write_text(
            json.dumps(
                [
                    {
                        "proposal_id": "P-1",
                        "decision": "promoted",
                        "decided_by": "LQ",
                        "decided_at": "2026-07-16T00:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        args = ["retro", "apply", "--retro", "retro-a", "--proposal", "P-1"]
        first = runner.invoke(main, args)
        assert first.exit_code == 0, first.output
        payload = json.loads(first.output.strip().splitlines()[-1])
        assert payload["applied"] == ["P-1"]
        assert payload["stage_dir"] is None
        target = root / ".aa" / "memory" / "aa-run.md"
        content = target.read_text(encoding="utf-8")
        assert "<!-- retro:retro-a#P-1 evidence:CH-1#F-1 -->" in content
        assert "- remember to check fixtures" in content

        second = runner.invoke(main, args)
        assert second.exit_code == 0, second.output
        assert target.read_text(encoding="utf-8") == content
        assert content.count("retro:retro-a#P-1") == 1


def test_retro_apply_stage_dir_does_not_touch_live() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-a")
        (retro_dir / "promotions.json").write_text(
            json.dumps(
                {
                    "schema_version": "2",
                    "events": [
                        {
                            "proposal_id": "P-1",
                            "type": "review_decision",
                            "decision": "promoted",
                            "actor": "LQ",
                            "at": "2026-07-16T00:00:00Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        stage = root / "stage-out"
        result = runner.invoke(
            main,
            ["retro", "apply", "--retro", "retro-a", "--proposal", "P-1", "--stage-dir", str(stage)],
        )
        assert result.exit_code == 0, result.output
        assert "retro:retro-a#P-1" in (stage / "aa-run.md").read_text(encoding="utf-8")
        assert not (root / ".aa" / "memory" / "aa-run.md").exists()


def test_retro_apply_rejects_unsafe_target() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        proposal = dict(_PROPOSAL, target="../escape.md")
        retro_dir = _seed_retro_dir(root, "retro-a", [proposal])
        (retro_dir / "promotions.json").write_text(
            json.dumps(
                [
                    {
                        "proposal_id": "P-1",
                        "decision": "promoted",
                        "decided_by": "LQ",
                        "decided_at": "2026-07-16T00:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["retro", "apply", "--retro", "retro-a", "--proposal", "P-1"])
        assert result.exit_code == 1
        assert "outside .aa/memory" in result.output
        assert not (root / "escape.md").exists()


def test_retro_apply_skips_unpromoted_proposals() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        _seed_retro_dir(root, "retro-a")
        result = runner.invoke(main, ["retro", "apply", "--retro", "retro-a"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["applied"] == []
        assert not (root / ".aa" / "memory" / "aa-run.md").exists()


def test_retro_rollback_deprecates_block_and_is_idempotent() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        retro_dir = _seed_retro_dir(root, "retro-r")
        (retro_dir / "promotions.json").write_text(
            json.dumps(
                [
                    {
                        "proposal_id": "P-1",
                        "decision": "promoted",
                        "decided_by": "LQ",
                        "decided_at": "2026-07-16T00:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        applied = runner.invoke(main, ["retro", "apply", "--retro", "retro-r", "--proposal", "P-1"])
        assert applied.exit_code == 0, applied.output
        target = root / ".aa" / "memory" / "aa-run.md"

        args = ["retro", "rollback", "--retro", "retro-r", "--proposal", "P-1", "--by", "phase-f"]
        first = runner.invoke(main, args)
        assert first.exit_code == 0, first.output
        payload = json.loads(first.output.strip().splitlines()[-1])
        assert payload["rolled_back"] is True
        assert "- deprecated: remember to check fixtures" in target.read_text(encoding="utf-8")
        raw = _read_promotions(retro_dir)
        assert raw["schema_version"] == "2"
        rollback_events = [
            e for e in raw["events"] if e["type"] == "application" and e["result"] == "rolled_back"
        ]
        assert len(rollback_events) == 1
        assert rollback_events[0]["actor"] == "phase-f"
        assert rollback_events[0]["target"] == ".aa/memory/aa-run.md"

        second = runner.invoke(main, args)
        assert second.exit_code == 0, second.output
        payload = json.loads(second.output.strip().splitlines()[-1])
        assert payload["rolled_back"] is False
        # no duplicate rollback event, file unchanged
        raw = _read_promotions(retro_dir)
        assert len([e for e in raw["events"] if e["type"] == "application"]) == 1


def test_retro_rollback_without_applied_block_errors() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        _seed_retro_dir(root, "retro-r")
        (root / ".aa" / "memory").mkdir(parents=True)
        (root / ".aa" / "memory" / "aa-run.md").write_text("# memory\n", encoding="utf-8")
        result = runner.invoke(
            main,
            ["retro", "rollback", "--retro", "retro-r", "--proposal", "P-1", "--by", "phase-f"],
        )
        assert result.exit_code == 1
        assert "applied block not found" in result.output
