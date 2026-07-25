from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.promotions import (
    append_promotion_events,
    application_event,
    effective_review_decisions,
    eval_completed_event,
    proposal_states,
    read_promotion_events,
    review_decision_event,
)


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_read_legacy_list_format(tmp_path: Path) -> None:
    retro = tmp_path / "retro"
    _write(
        retro / "promotions.json",
        [
            {
                "proposal_id": "P-1",
                "decision": "promoted",
                "decided_by": "LQ",
                "decided_at": "2026-07-16T00:00:00Z",
                "rework_note": None,
                "eval_run_id": "eval-1",
            }
        ],
    )
    events = read_promotion_events(retro)
    assert events == [
        {
            "proposal_id": "P-1",
            "type": "review_decision",
            "decision": "promoted",
            "actor": "LQ",
            "at": "2026-07-16T00:00:00Z",
            "eval_run_id": "eval-1",
        }
    ]


def test_read_legacy_promotions_dict_format(tmp_path: Path) -> None:
    retro = tmp_path / "retro"
    _write(
        retro / "promotions.json",
        {
            "promotions": [
                {
                    "proposal_id": "P-2",
                    "decision": "needs_rework",
                    "decided_by": "driver",
                    "decided_at": "2026-07-16T01:00:00Z",
                    "rework_note": "thin evidence",
                }
            ]
        },
    )
    events = read_promotion_events(retro)
    assert len(events) == 1
    assert events[0]["type"] == "review_decision"
    assert events[0]["decision"] == "needs_rework"
    assert events[0]["actor"] == "driver"
    assert events[0]["rework_note"] == "thin evidence"


def test_read_v2_events_format_roundtrip(tmp_path: Path) -> None:
    retro = tmp_path / "retro"
    stream = [
        review_decision_event("P-1", decision="promoted", actor="LQ", at="2026-07-16T00:00:00Z"),
        eval_completed_event("P-1", result="pass", actor="aa", at="2026-07-16T00:01:00Z", run_ids=["eval-1"]),
        application_event(
            "P-1",
            result="applied",
            actor="aa",
            at="2026-07-16T00:02:00Z",
            target=".aa/memory/aa-run.md",
            content_sha256="abc",
        ),
    ]
    _write(retro / "promotions.json", {"schema_version": "2", "events": stream})
    assert read_promotion_events(retro) == stream


def test_read_missing_or_malformed_returns_empty(tmp_path: Path) -> None:
    retro = tmp_path / "retro"
    assert read_promotion_events(retro) == []
    retro.mkdir(parents=True)
    (retro / "promotions.json").write_text("{not json", encoding="utf-8")
    assert read_promotion_events(retro) == []


def test_append_writes_v2_atomically_and_upgrades_legacy(tmp_path: Path) -> None:
    retro = tmp_path / "retro"
    _write(
        retro / "promotions.json",
        [
            {
                "proposal_id": "P-0",
                "decision": "rejected",
                "decided_by": "LQ",
                "decided_at": "2026-07-15T00:00:00Z",
            }
        ],
    )
    stream = append_promotion_events(retro, [review_decision_event("P-1", decision="promoted", actor="LQ")])
    assert len(stream) == 2
    raw = json.loads((retro / "promotions.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == "2"
    assert [e["proposal_id"] for e in raw["events"]] == ["P-0", "P-1"]
    # legacy record preserved as a coerced review_decision event
    assert raw["events"][0]["type"] == "review_decision"
    assert raw["events"][0]["decision"] == "rejected"
    # atomic write leaves no temp files behind
    assert [p.name for p in retro.iterdir()] == ["promotions.json"]


def test_proposal_states_machine(tmp_path: Path) -> None:
    events = [
        review_decision_event("P-ok", decision="promoted", actor="LQ"),
        review_decision_event("P-no", decision="rejected", actor="LQ"),
        review_decision_event("P-fix", decision="needs_rework", actor="LQ"),
        review_decision_event("P-applied", decision="promoted", actor="LQ"),
        eval_completed_event("P-applied", result="pass", actor="aa", run_ids=["r1"]),
        application_event("P-applied", result="applied", actor="aa"),
        review_decision_event("P-rb", decision="promoted", actor="LQ"),
        eval_completed_event("P-rb", result="regression", actor="aa", run_ids=["r2"]),
        application_event("P-rb", result="rolled_back", actor="aa"),
        review_decision_event("P-inc", decision="promoted", actor="LQ"),
        eval_completed_event("P-inc", result="inconclusive", actor="aa"),
        review_decision_event("P-err", decision="promoted", actor="LQ"),
        eval_completed_event("P-err", result="error", actor="aa"),
    ]
    states = proposal_states(events)
    assert states["P-ok"] == "promoted_pending_eval"
    assert states["P-no"] == "rejected"
    assert states["P-fix"] == "needs_rework"
    assert states["P-applied"] == "applied"
    assert states["P-rb"] == "rolled_back"
    assert states["P-inc"] == "awaiting_baseline"
    assert states["P-err"] == "eval_error"


def test_effective_review_decisions_last_wins() -> None:
    events = [
        review_decision_event("P-1", decision="needs_rework", actor="a"),
        review_decision_event("P-1", decision="promoted", actor="b"),
        eval_completed_event("P-1", result="pass", actor="aa"),
    ]
    decisions = effective_review_decisions(events)
    assert decisions["P-1"]["decision"] == "promoted"
    assert decisions["P-1"]["actor"] == "b"


def test_knowledge_proposal_acceptance_leaves_data_knowledge_untouched(tmp_path: Path) -> None:
    """Retro accept validates domain_knowledge proposals but never writes L1 knowledge."""
    from tests.helpers_aa import write_aa_config
    from tests.unit.retro.archive_fixtures import make_archived_change
    from tests.unit.retro.issue_fixtures import write_change_occurrence_detected, write_project_problem_lifecycle

    write_aa_config(tmp_path)
    root = make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    issue_evidence = write_change_occurrence_detected(root, change_id="CH-1")
    write_project_problem_lifecycle(tmp_path, change_id="CH-1")

    from assurance_agent.retro.collect_stage import run_retro_collect
    from assurance_agent.retro.accept_stage import run_retro_accept
    from assurance_agent.workflow.core.templates import build_data_knowledge_yaml

    run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    retro_dir = tmp_path / "qa" / "retro" / "retro-test"
    context = json.loads((retro_dir / "context.json").read_text())
    resolved_id = next(
        signal["evidence_ids"][0]
        for signal in context["signals"]["problem_resolutions"]
        if signal["outcome"] == "resolved"
    )

    knowledge_path = tmp_path / ".aa" / "data-knowledge.yaml"
    knowledge_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_path.write_text(build_data_knowledge_yaml(), encoding="utf-8")
    before = knowledge_path.read_text(encoding="utf-8")
    issues_before = (tmp_path / "qa" / "issues" / "events.jsonl").read_text(encoding="utf-8")

    (retro_dir / "proposals.json").write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "id": "P-KNOW",
                        "finding_kind": "domain_knowledge",
                        "apply_kind": "knowledge_delta",
                        "payload": {
                            "schema_version": "1",
                            "mode": "delta",
                            "based_on_l1_version": 1,
                            "auth": {
                                "api_admin_token": {
                                    "method": "token",
                                    "notes": "retro discovered admin token helper",
                                }
                            },
                        },
                        "problem": "resolved product bug exposed missing auth capability",
                        "evidence_ids": [resolved_id, issue_evidence],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")

    proposals = run_retro_accept(tmp_path, retro_id="retro-test", min_evidence=1)

    assert len(proposals) == 1
    assert proposals[0].finding_kind == "domain_knowledge"
    assert knowledge_path.read_text(encoding="utf-8") == before
    assert (tmp_path / "qa" / "issues" / "events.jsonl").read_text(encoding="utf-8") == issues_before
