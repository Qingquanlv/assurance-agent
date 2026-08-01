"""Pure change-location decision and D17 evidence replay tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.change_location import (
    ChangeLocationError,
    ChangeLocationProbe,
    ChangeNotFoundError,
    decide_change_location,
    parse_change_roots,
    probe_change_location_candidates,
    resolve_change,
)
from assurance_agent.eval.change_location_evidence import (
    ChangeLocationEvidenceError,
    build_change_location_evidence,
    replay_change_location_evidence,
)
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def _config_bytes(*, changes: str = "./qa/changes", archive: str = "./qa/archive") -> bytes:
    text = build_config_yaml(InitAnswers())
    text = text.replace("  changes: ./qa/changes\n", f"  changes: {changes}\n")
    text = text.replace("  archive: ./qa/archive\n", f"  archive: {archive}\n")
    return text.encode("utf-8")


def _write_config(root: Path, *, changes: str = "./qa/changes", archive: str = "./qa/archive") -> bytes:
    (root / ".aa").mkdir(exist_ok=True)
    raw = _config_bytes(changes=changes, archive=archive)
    (root / ".aa" / "config.yaml").write_bytes(raw)
    return raw


def test_parse_change_roots_default_and_custom() -> None:
    roots = parse_change_roots(_config_bytes())
    assert roots.changes_root == "qa/changes"
    assert roots.archive_root == "qa/archive"
    custom = parse_change_roots(_config_bytes(changes="./work/changes", archive="./work/archive"))
    assert custom.changes_root == "work/changes"
    assert custom.archive_root == "work/archive"


@pytest.mark.parametrize(
    "changes,archive",
    [
        ("/abs/changes", "./qa/archive"),
        ("./qa/changes", "/abs/archive"),
        ("../escape/changes", "./qa/archive"),
        ("./qa/changes", "../escape/archive"),
    ],
)
def test_parse_change_roots_rejects_absolute_and_parent(changes: str, archive: str) -> None:
    with pytest.raises(ChangeLocationError):
        parse_change_roots(_config_bytes(changes=changes, archive=archive))


def test_decide_coexistence_selects_changes_for_active(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    (tmp_path / "qa" / "archive" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    decided = decide_change_location(
        change_id="CH-1", preference="active", roots=roots, probes=probes
    )
    assert decided.source == "changes"
    assert decided.repo_path == "qa/changes/CH-1"
    assert probes[0].lstat_kind == "directory"
    assert probes[1].lstat_kind == "directory"


def test_decide_archive_only_rejects_active(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "archive" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    with pytest.raises(ChangeNotFoundError, match="active change"):
        decide_change_location(change_id="CH-1", preference="active", roots=roots, probes=probes)


def test_decide_missing_active_leaf(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    assert probes[0].lstat_kind == "missing"
    with pytest.raises(ChangeNotFoundError, match="not found"):
        decide_change_location(change_id="CH-1", preference="active", roots=roots, probes=probes)


def test_probe_symlink_candidate_is_not_directory(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    target = tmp_path / "elsewhere" / "CH-1"
    target.mkdir(parents=True)
    link = tmp_path / "qa" / "changes" / "CH-1"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    assert probes[0].lstat_kind == "symlink"
    assert probes[0].mode is not None
    with pytest.raises(ChangeNotFoundError):
        decide_change_location(change_id="CH-1", preference="active", roots=roots, probes=probes)


def test_resolve_change_delegates_to_pure_seams(tmp_path: Path) -> None:
    _write_config(tmp_path, changes="./work/changes", archive="./work/archive")
    change = tmp_path / "work" / "changes" / "CH-9"
    change.mkdir(parents=True)
    loc = resolve_change(tmp_path, "CH-9")
    assert loc.path == change
    assert loc.source == "changes"


def test_build_and_replay_evidence_byte_identical(tmp_path: Path) -> None:
    raw = _write_config(tmp_path, changes="./work/changes", archive="./work/archive")
    (tmp_path / "work" / "changes" / "CH-1").mkdir(parents=True)
    (tmp_path / "work" / "archive" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    leaves = {
        "work/changes/CH-1/proposal.md",
        "work/archive/CH-1/proposal.md",
        "work/changes/OTHER/proposal.md",
    }
    evidence = build_change_location_evidence(
        change_id="CH-1",
        config_bytes=raw,
        probes=probes,
        before_manifest_leaves=leaves,
    )
    assert evidence.resolved_change_repo_path == "work/changes/CH-1"
    assert evidence.candidates[0].has_before_manifest_leaf is True
    assert evidence.candidates[1].has_before_manifest_leaf is True
    assert evidence.candidates[0].selected is True
    assert evidence.candidates[1].selected is False

    replayed = replay_change_location_evidence(
        evidence=evidence,
        config_bytes=raw,
        before_manifest_leaves=leaves,
    )
    assert canonical_json_bytes(replayed) == canonical_json_bytes(evidence)


def test_replay_rejects_tampered_config_digest(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    evidence = build_change_location_evidence(
        change_id="CH-1",
        config_bytes=raw,
        probes=probes,
        before_manifest_leaves={"qa/changes/CH-1/a.md"},
    )
    tampered = raw + b"\n# tamper\n"
    with pytest.raises(ChangeLocationEvidenceError, match="config digest"):
        replay_change_location_evidence(
            evidence=evidence,
            config_bytes=tampered,
            before_manifest_leaves={"qa/changes/CH-1/a.md"},
        )


def test_build_rejects_missing_or_extra_candidates(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    only_one = (probes[0],)
    with pytest.raises(ChangeLocationEvidenceError, match="exactly"):
        build_change_location_evidence(
            change_id="CH-1",
            config_bytes=raw,
            probes=only_one,
            before_manifest_leaves=set(),
        )
    extra = (
        probes[0],
        probes[1],
        ChangeLocationProbe(
            source="changes",
            configured_root=roots.changes_root,
            candidate_repo_path="qa/changes/OTHER",
            lstat_kind="directory",
            mode=probes[0].mode,
        ),
    )
    with pytest.raises(ChangeLocationEvidenceError, match="exactly"):
        build_change_location_evidence(
            change_id="CH-1",
            config_bytes=raw,
            probes=extra,
            before_manifest_leaves=set(),
        )


def test_build_rejects_archive_only(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "archive" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    with pytest.raises(ChangeLocationEvidenceError):
        build_change_location_evidence(
            change_id="CH-1",
            config_bytes=raw,
            probes=probes,
            before_manifest_leaves=set(),
        )


def test_leaf_bit_ignores_another_change(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    evidence = build_change_location_evidence(
        change_id="CH-1",
        config_bytes=raw,
        probes=probes,
        before_manifest_leaves={"qa/changes/OTHER/proposal.md"},
    )
    assert evidence.candidates[0].has_before_manifest_leaf is False


def test_probe_mode_matches_lstat(tmp_path: Path) -> None:
    raw = _write_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    roots = parse_change_roots(raw)
    probes = probe_change_location_candidates(tmp_path, "CH-1", roots)
    st = os.lstat(change)
    assert probes[0].mode == int(st.st_mode)
    assert probes[0].lstat_kind == "directory"
