"""Apply retro proposals into a staging directory (memory overlay)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.proposals import read_proposals
from assurance_agent.retro.types import RetroProposal, memory_body_text


def apply_proposal_to_stage(
    *,
    sut_root: Path,
    retro_id: str,
    proposal_id: str,
    stage_dir: Path,
) -> RetroProposal:
    """Materialize a ``memory_append`` proposal into ``stage_dir`` as a memory overlay."""
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut_root / "qa" / "retro" / retro_id
    proposals = read_proposals(retro_dir)
    proposal = next((p for p in proposals if p.id == proposal_id), None)
    if proposal is None:
        raise AaError(f"proposal not found: {proposal_id}")
    if proposal.apply_kind != "memory_append":
        raise AaError(
            f"proposal {proposal_id} apply_kind={proposal.apply_kind!r} cannot be staged "
            "(only memory_append is supported)"
        )

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    memory_dir = stage_dir / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    body = memory_body_text(proposal)
    (memory_dir / f"{proposal.id}.md").write_text(body + "\n", encoding="utf-8")
    (stage_dir / "proposal-meta.json").write_text(
        json.dumps(proposal.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return proposal


def resolve_memory_target(sut_root: Path, target: str | None) -> Path:
    """Resolve a proposal memory target, enforcing the ``.aa/memory/`` sandbox.

    Rejects empty targets, absolute paths, escapes outside ``.aa/memory/``
    (including ``..``), symlinks and non-regular files (spec §6).
    """
    if not target or not target.strip():
        raise AaError("proposal target is empty (expected a path under .aa/memory/)")
    raw = sut_root / target
    if Path(target).is_absolute():
        raise AaError(f"unsafe memory target (absolute path): {target}")
    memory_root = (sut_root / ".aa" / "memory").resolve()
    resolved = raw.resolve()
    if resolved != memory_root and memory_root not in resolved.parents:
        raise AaError(f"unsafe memory target (outside .aa/memory/): {target}")
    if raw.is_symlink():
        raise AaError(f"unsafe memory target (symlink): {target}")
    if resolved.exists() and not resolved.is_file():
        raise AaError(f"unsafe memory target (not a regular file): {target}")
    return resolved


def retro_marker(retro_id: str, proposal_id: str) -> str:
    return f"retro:{retro_id}#{proposal_id}"


def apply_memory_proposal(
    sut_root: Path,
    retro_id: str,
    proposal: RetroProposal,
    stage_dir: Path | None = None,
) -> bool:
    """Append the proposal block to its memory target, marker-idempotent.

    Live mode (``stage_dir=None``) writes the real ``.aa/memory/<target>``;
    stage mode renders "current live content + new block" into
    ``<stage_dir>/<basename(target)>`` without touching the live file.
    The block carries a ``<!-- retro:<id>#<proposal> evidence:... -->`` marker
    so the same proposal appears at most once in either destination.
    Returns True when the block was newly appended.
    """
    marker = retro_marker(retro_id, proposal.id)
    live_target = resolve_memory_target(sut_root, proposal.target)
    live_content = live_target.read_text(encoding="utf-8") if live_target.is_file() else ""

    if stage_dir is not None:
        out_path = stage_dir / live_target.name
        base = out_path.read_text(encoding="utf-8") if out_path.is_file() else live_content
    else:
        out_path = live_target
        base = live_content
    if marker in base:
        if stage_dir is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(base, encoding="utf-8")
        return False

    change = memory_body_text(proposal)
    evidence = ",".join(proposal.evidence_ids)
    block = f"\n<!-- {marker} evidence:{evidence} -->\n- {change}\n<!-- /retro -->\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(base + block, encoding="utf-8")
    return True


def deprecate_memory_block(sut_root: Path, retro_id: str, proposal: RetroProposal) -> bool:
    """Mark the block written by :func:`apply_memory_proposal` as deprecated.

    Prefixes every ``- `` line inside the marker block with ``deprecated: ``
    (no physical delete). Returns True when the file changed; False when the
    block was already deprecated (idempotent).
    """
    marker = retro_marker(retro_id, proposal.id)
    target = resolve_memory_target(sut_root, proposal.target)
    if not target.is_file():
        raise AaError(f"memory file not found: {proposal.target}")
    lines = target.read_text(encoding="utf-8").split("\n")
    start = next((i for i, line in enumerate(lines) if f"<!-- {marker}" in line), None)
    if start is None:
        raise AaError(f"applied block not found for {marker}")

    changed = False
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "<!-- /retro -->":
            break
        if lines[i].startswith("- ") and not lines[i].startswith("- deprecated: "):
            lines[i] = f"- deprecated: {lines[i][2:]}"
            changed = True
    if changed:
        target.write_text("\n".join(lines), encoding="utf-8")
    return changed


def improvement_marker(improvement_id: str) -> str:
    """Canonical Improvement memory marker token (without HTML comment wrappers)."""
    return f"improvement:{improvement_id}"


def apply_improvement_memory_patch(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
    proposed_change: str,
    evidence_ids: list[str] | tuple[str, ...],
    stage_dir: Path | None = None,
) -> bool:
    """Append an Improvement-scoped memory block, marker-idempotent.

    Marker form: ``<!-- improvement:<id> evidence:<sorted-source-ids> -->``.
    Live mode writes the real target; stage mode writes basename under ``stage_dir``
    without mutating the live file. Returns True when newly appended.
    """
    marker = improvement_marker(improvement_id)
    live_target = resolve_memory_target(sut_root, target)
    live_content = live_target.read_text(encoding="utf-8") if live_target.is_file() else ""

    if stage_dir is not None:
        out_path = stage_dir / live_target.name
        base = out_path.read_text(encoding="utf-8") if out_path.is_file() else live_content
    else:
        out_path = live_target
        base = live_content
    if marker in base:
        if stage_dir is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(base, encoding="utf-8")
        return False

    evidence = ",".join(sorted(evidence_ids))
    block = (
        f"\n<!-- {marker} evidence:{evidence} -->\n"
        f"- {proposed_change}\n"
        f"<!-- /improvement -->\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(base + block, encoding="utf-8")
    return True


def deprecate_improvement_memory_block(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
) -> bool:
    """Deprecate the block written by :func:`apply_improvement_memory_patch`."""
    marker = improvement_marker(improvement_id)
    path = resolve_memory_target(sut_root, target)
    if not path.is_file():
        raise AaError(f"memory file not found: {target}")
    lines = path.read_text(encoding="utf-8").split("\n")
    start = next((i for i, line in enumerate(lines) if f"<!-- {marker}" in line), None)
    if start is None:
        raise AaError(f"applied block not found for {marker}")

    changed = False
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "<!-- /improvement -->":
            break
        if lines[i].startswith("- ") and not lines[i].startswith("- deprecated: "):
            lines[i] = f"- deprecated: {lines[i][2:]}"
            changed = True
    if changed:
        path.write_text("\n".join(lines), encoding="utf-8")
    return changed


def extract_improvement_block_bytes(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
) -> bytes:
    """Return the exact applied Improvement block bytes (including markers)."""
    marker = improvement_marker(improvement_id)
    path = resolve_memory_target(sut_root, target)
    if not path.is_file():
        raise AaError(f"memory file not found: {target}")
    text = path.read_text(encoding="utf-8")
    start = text.find(f"<!-- {marker}")
    if start < 0:
        raise AaError(f"applied block not found for {marker}")
    end_token = "<!-- /improvement -->"
    end = text.find(end_token, start)
    if end < 0:
        raise AaError(f"applied block close marker missing for {marker}")
    return text[start : end + len(end_token)].encode("utf-8")
