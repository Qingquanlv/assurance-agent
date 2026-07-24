"""Read-only JSON projections of retro runs for external consumers (e.g. UI).

The ``aa retro list``/``aa retro show`` commands expose these projections so
frontends never re-implement proposal state folding or parse retro files
directly. The state machine (:func:`proposal_states`) and file readers remain
the single source of truth; this module only shapes their output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assurance_agent.retro.promotions import (
    EVENT_APPLICATION,
    EVENT_EVAL_COMPLETED,
    EVENT_PROPOSAL_EXPORTED,
    EVENT_REVIEW_DECISION,
    proposal_states,
    read_promotion_events,
)
from assurance_agent.retro.proposals import read_proposals

RETRO_DIR_PREFIX = "retro-"


def _retro_root(sut: Path) -> Path:
    return sut / "qa" / "retro"


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_context(retro_dir: Path, retro_id: str) -> dict:
    raw = _read_json(retro_dir / "context.json")
    if not isinstance(raw, dict):
        return {"retro_id": retro_id, "generated_at": None, "change_ids": []}
    window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    change_ids = [c for c in window.get("change_ids", []) if isinstance(c, str)]
    return {
        "retro_id": raw.get("retro_id") if isinstance(raw.get("retro_id"), str) else retro_id,
        "generated_at": raw.get("generated_at") if isinstance(raw.get("generated_at"), str) else None,
        "change_ids": change_ids,
    }


def _eval_results_by_proposal(retro_dir: Path) -> dict[str, list[dict]]:
    raw = _read_json(retro_dir / "eval-results.json")
    results = raw.get("results") if isinstance(raw, dict) else None
    by_proposal: dict[str, list[dict]] = {}
    if not isinstance(results, list):
        return by_proposal
    for result in results:
        if not isinstance(result, dict):
            continue
        mapped = {
            "suite": result.get("suite"),
            "eval_run_id": result.get("eval_run_id"),
            "verdict": result.get("verdict"),
            "auto_apply": result.get("auto_apply"),
            "note": result.get("note"),
        }
        for pid in result.get("proposal_ids", []):
            if isinstance(pid, str):
                by_proposal.setdefault(pid, []).append(mapped)
    return by_proposal


def _timeline_event(event: dict) -> dict:
    base = {"type": event.get("type"), "actor": event.get("actor"), "at": event.get("at")}
    if event.get("type") == EVENT_REVIEW_DECISION:
        base["decision"] = event.get("decision")
        base["rework_note"] = event.get("rework_note")
    elif event.get("type") == EVENT_EVAL_COMPLETED:
        base["result"] = event.get("result")
        base["run_ids"] = event.get("run_ids", [])
        base["gate"] = event.get("gate")
        base["note"] = event.get("note")
    elif event.get("type") == EVENT_APPLICATION:
        base["result"] = event.get("result")
        base["target"] = event.get("target")
        base["content_sha256"] = event.get("content_sha256")
        base["note"] = event.get("note")
    elif event.get("type") == EVENT_PROPOSAL_EXPORTED:
        base["target"] = event.get("target")
        base["source_sha256"] = event.get("source_sha256")
    return base


def list_retro_ids(sut: Path) -> list[str]:
    root = _retro_root(sut)
    if not root.is_dir():
        return []
    ids = [p.name for p in root.iterdir() if p.is_dir() and p.name.startswith(RETRO_DIR_PREFIX)]
    ids.sort(reverse=True)
    return ids


def project_retro_list(sut: Path) -> dict:
    """Summary projection of every retro run under ``qa/retro/``."""
    runs: list[dict] = []
    for retro_id in list_retro_ids(sut):
        retro_dir = _retro_root(sut) / retro_id
        context = _read_context(retro_dir, retro_id)
        proposals = read_proposals(retro_dir)
        events = read_promotion_events(retro_dir)
        states = proposal_states(events)

        proposal_ids = [p.id for p in proposals]
        for event in events:
            pid = event.get("proposal_id")
            if isinstance(pid, str) and pid not in proposal_ids:
                proposal_ids.append(pid)

        status_counts: dict[str, int] = {}
        for pid in proposal_ids:
            state = states.get(pid, "proposed")
            status_counts[state] = status_counts.get(state, 0) + 1

        runs.append(
            {
                "retro_id": context["retro_id"],
                "generated_at": context["generated_at"],
                "proposal_count": len(proposals),
                "change_ids": context["change_ids"],
                "status_counts": status_counts,
            }
        )
    return {"runs": runs}


def project_retro_show(sut: Path, retro_id: str) -> dict:
    """Detailed projection of one retro run: proposals + folded state + timeline."""
    retro_dir = _retro_root(sut) / retro_id
    context = _read_context(retro_dir, retro_id)
    proposals = read_proposals(retro_dir)
    events = read_promotion_events(retro_dir)
    states = proposal_states(events)
    eval_by_proposal = _eval_results_by_proposal(retro_dir)

    grouped: dict[str, list[dict]] = {}
    export_meta: dict[str, dict[str, str | None]] = {}
    for event in events:
        pid = event.get("proposal_id")
        if isinstance(pid, str):
            grouped.setdefault(pid, []).append(event)
            if event.get("type") == EVENT_PROPOSAL_EXPORTED:
                export_meta[pid] = {
                    "target_path": event.get("target") if isinstance(event.get("target"), str) else None,
                    "source_sha256": (
                        event.get("source_sha256")
                        if isinstance(event.get("source_sha256"), str)
                        else None
                    ),
                }

    proposal_map = {p.id: p for p in proposals}
    all_ids = set(proposal_map)
    all_ids.update(grouped)

    mapped_proposals: list[dict] = []
    for pid in sorted(all_ids):
        proposal = proposal_map.get(pid)
        mapped_proposals.append(
            {
                "id": pid,
                "layer": proposal.layer if proposal else None,
                "finding_kind": proposal.finding_kind if proposal else None,
                "target": proposal.target if proposal else None,
                "problem": proposal.problem if proposal else None,
                "proposed_change": proposal.proposed_change if proposal else None,
                "apply_kind": proposal.apply_kind if proposal else None,
                "eval_suite": proposal.eval_suite if proposal else None,
                "risk": proposal.risk if proposal else None,
                "confidence": proposal.confidence if proposal else None,
                "state": states.get(pid, "proposed"),
                "evidence_ids": list(proposal.evidence_ids) if proposal else [],
                "export_target_path": export_meta.get(pid, {}).get("target_path"),
                "export_source_sha256": export_meta.get(pid, {}).get("source_sha256"),
                "timeline": [_timeline_event(e) for e in grouped.get(pid, [])],
                "eval_results": eval_by_proposal.get(pid, []),
            }
        )

    return {
        "retro_id": context["retro_id"],
        "generated_at": context["generated_at"],
        "change_ids": context["change_ids"],
        "proposals": mapped_proposals,
    }


def project_proposals_for_change(sut: Path, change_id: str) -> dict:
    """Reverse index: proposals whose evidence cites ``change_id``."""
    prefix = f"{change_id}#"
    matches: list[dict] = []
    for retro_id in list_retro_ids(sut):
        retro_dir = _retro_root(sut) / retro_id
        proposals = read_proposals(retro_dir)
        if not proposals:
            continue
        states = proposal_states(read_promotion_events(retro_dir))
        for proposal in proposals:
            matching = [e for e in proposal.evidence_ids if e.startswith(prefix)]
            if not matching:
                continue
            matches.append(
                {
                    "retro_id": retro_id,
                    "id": proposal.id,
                    "problem": proposal.problem or None,
                    "target": proposal.target,
                    "state": states.get(proposal.id, "proposed"),
                    "matching_evidence_count": len(matching),
                }
            )
    matches.sort(key=lambda m: (m["retro_id"], m["id"]), reverse=False)
    matches.sort(key=lambda m: m["retro_id"], reverse=True)
    return {"change_id": change_id, "proposals": matches}
