from __future__ import annotations

from pathlib import Path

from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.projection import (
    project_proposals_for_change,
    project_retro_list,
    project_retro_show,
)


def _proposal(pid: str, *, evidence: list[str], suite: str = "workflow-full") -> dict:
    return {
        "id": pid,
        "finding_kind": "prompt_rule",
        "apply_kind": "memory_append",
        "payload": {"body": f"remember {pid}"},
        "eval_suite": suite,
        "status": "proposed",
        "layer": "agent",
        "target": f".aa/memory/{pid}.md",
        "problem": f"problem for {pid}",
        "proposed_change": f"remember {pid}",
        "risk": "low",
        "confidence": "high",
        "evidence_ids": evidence,
    }


def _seed(
    sut: Path,
    retro_id: str,
    *,
    proposals: list[dict],
    change_ids: list[str],
    events: list[dict] | None = None,
    eval_results: list[dict] | None = None,
) -> Path:
    retro = sut / "qa" / "retro" / retro_id
    retro.mkdir(parents=True, exist_ok=True)
    write_json(retro / "proposals.json", {"proposals": proposals})
    write_json(
        retro / "context.json",
        {
            "retro_id": retro_id,
            "generated_at": "2026-07-22T03:57:02Z",
            "window": {"change_count": len(change_ids), "change_ids": change_ids, "change_sources": []},
            "signals": {},
            "signal_count": 0,
        },
    )
    if events is not None:
        write_json(retro / "promotions.json", {"schema_version": "2", "events": events})
    if eval_results is not None:
        write_json(retro / "eval-results.json", {"results": eval_results})
    return retro


def test_list_folds_status_counts(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "retro-20260722-035702",
        proposals=[
            _proposal("RETRO-001", evidence=["CH-A#FAIL-1"]),
            _proposal("RETRO-002", evidence=["CH-B#FAIL-2"]),
        ],
        change_ids=["CH-A", "CH-B"],
        events=[
            {"proposal_id": "RETRO-001", "type": "review_decision", "decision": "promoted", "actor": "LQ", "at": "t"},
            {"proposal_id": "RETRO-001", "type": "eval_completed", "result": "pass", "run_ids": ["e1"], "actor": "gate", "at": "t"},
            {"proposal_id": "RETRO-001", "type": "application", "result": "applied", "actor": "gate", "at": "t"},
            {"proposal_id": "RETRO-002", "type": "review_decision", "decision": "rejected", "actor": "LQ", "at": "t"},
        ],
    )
    payload = project_retro_list(tmp_path)
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["retro_id"] == "retro-20260722-035702"
    assert run["proposal_count"] == 2
    assert run["change_ids"] == ["CH-A", "CH-B"]
    assert run["status_counts"] == {"applied": 1, "rejected": 1}


def test_list_sorts_newest_first(tmp_path: Path) -> None:
    _seed(tmp_path, "retro-20260701-000000", proposals=[], change_ids=[])
    _seed(tmp_path, "retro-20260722-035702", proposals=[], change_ids=[])
    runs = project_retro_list(tmp_path)["runs"]
    assert [r["retro_id"] for r in runs] == ["retro-20260722-035702", "retro-20260701-000000"]


def test_list_empty_when_no_retro_dir(tmp_path: Path) -> None:
    assert project_retro_list(tmp_path) == {"runs": []}


def test_show_maps_proposal_state_timeline_and_eval(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "retro-20260722-035702",
        proposals=[_proposal("RETRO-001", evidence=["CH-A#FAIL-1", "CH-B#FAIL-2"])],
        change_ids=["CH-A", "CH-B"],
        events=[
            {"proposal_id": "RETRO-001", "type": "review_decision", "decision": "promoted", "actor": "LQ", "at": "t1"},
            {"proposal_id": "RETRO-001", "type": "eval_completed", "result": "pass", "run_ids": ["e1"], "gate": "pass", "actor": "gate", "at": "t2"},
            {"proposal_id": "RETRO-001", "type": "application", "result": "applied", "target": ".aa/memory/RETRO-001.md", "actor": "gate", "at": "t3"},
        ],
        eval_results=[{"suite": "workflow-full", "eval_run_id": "e1", "verdict": "pass", "auto_apply": False, "proposal_ids": ["RETRO-001"]}],
    )
    payload = project_retro_show(tmp_path, "retro-20260722-035702")
    assert payload["change_ids"] == ["CH-A", "CH-B"]
    assert len(payload["proposals"]) == 1
    p = payload["proposals"][0]
    assert p["id"] == "RETRO-001"
    assert p["state"] == "applied"
    assert p["risk"] == "low"
    assert p["target"] == ".aa/memory/RETRO-001.md"
    assert [e["type"] for e in p["timeline"]] == ["review_decision", "eval_completed", "application"]
    assert p["timeline"][0]["decision"] == "promoted"
    assert p["eval_results"][0]["eval_run_id"] == "e1"


def test_show_includes_orphan_event_proposal(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "retro-20260722-035702",
        proposals=[],
        change_ids=[],
        events=[{"proposal_id": "RETRO-ORPHAN", "type": "review_decision", "decision": "rejected", "actor": "LQ", "at": "t"}],
    )
    payload = project_retro_show(tmp_path, "retro-20260722-035702")
    assert [p["id"] for p in payload["proposals"]] == ["RETRO-ORPHAN"]
    assert payload["proposals"][0]["state"] == "rejected"


def test_proposals_for_change_matches_by_evidence_prefix(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "retro-20260722-035702",
        proposals=[
            _proposal("RETRO-001", evidence=["CH-A#FAIL-1", "CH-B#FAIL-2"]),
            _proposal("RETRO-002", evidence=["CH-C#FAIL-9"]),
        ],
        change_ids=["CH-A", "CH-B", "CH-C"],
        events=[],
    )
    payload = project_proposals_for_change(tmp_path, "CH-A")
    assert payload["change_id"] == "CH-A"
    assert [p["id"] for p in payload["proposals"]] == ["RETRO-001"]
    assert payload["proposals"][0]["matching_evidence_count"] == 1


def test_proposals_for_change_no_match(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "retro-20260722-035702",
        proposals=[_proposal("RETRO-001", evidence=["CH-A#FAIL-1"])],
        change_ids=["CH-A"],
        events=[],
    )
    assert project_proposals_for_change(tmp_path, "CH-ZZZ")["proposals"] == []


def test_projection_folds_legacy_promotions_format(tmp_path: Path) -> None:
    """A pre-schema-2 promotions.json (bare RetroPromoteRecord list) must fold
    to the same states/timeline as the current event stream, since projection
    delegates to read_promotion_events (which upgrades legacy records)."""
    retro = _seed(
        tmp_path,
        "retro-20260713-000000",
        proposals=[
            _proposal("RETRO-001", evidence=["CH-A#FAIL-1"]),
            _proposal("RETRO-002", evidence=["CH-B#FAIL-2"]),
        ],
        change_ids=["CH-A", "CH-B"],
    )
    # Legacy format: a bare list of RetroPromoteRecord dicts (no schema_version,
    # no "events" key, "type" is implicit review_decision).
    write_json(
        retro / "promotions.json",
        [
            {
                "proposal_id": "RETRO-001",
                "decision": "promoted",
                "decided_by": "LQ",
                "decided_at": "2026-07-13T00:01:00Z",
            },
            {
                "proposal_id": "RETRO-002",
                "decision": "needs_rework",
                "decided_by": "LQ",
                "decided_at": "2026-07-13T00:02:00Z",
                "rework_note": "tighten the assertion",
            },
        ],
    )

    counts = project_retro_list(tmp_path)["runs"][0]["status_counts"]
    assert counts == {"promoted_pending_eval": 1, "needs_rework": 1}

    payload = project_retro_show(tmp_path, "retro-20260713-000000")
    by_id = {p["id"]: p for p in payload["proposals"]}
    assert by_id["RETRO-001"]["state"] == "promoted_pending_eval"
    assert by_id["RETRO-002"]["state"] == "needs_rework"
    # Legacy records are coerced into review_decision timeline events, and the
    # rework_note survives the coercion.
    timeline = by_id["RETRO-002"]["timeline"]
    assert [e["type"] for e in timeline] == ["review_decision"]
    assert timeline[0]["decision"] == "needs_rework"
    assert timeline[0]["rework_note"] == "tighten the assertion"
