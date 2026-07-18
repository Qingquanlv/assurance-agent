"""Eval fixture tiers and change seeding.

Semantic port of the TypeScript ``seed_change`` / ``fixture_utils`` flow:
load a tier manifest (with ``extends`` chain), copy golden sample paths into
an isolated SUT sandbox, then reset ``workflow-state.yaml`` via
``read_state`` / ``write_state`` so integrity hashes stay valid for audits.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.state import read_state_lenient, write_state


class FixtureResets(BaseModel):
    workflow_state: dict[str, Any] = Field(default_factory=dict)
    qa_yaml: dict[str, Any] = Field(default_factory=dict)


class TierManifest(BaseModel):
    name: str
    extends: str | None = None
    description: str = ""
    paths: list[str] = Field(default_factory=list)
    resets: FixtureResets = Field(default_factory=FixtureResets)
    source_prefix: str | None = None


class FixtureLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_dir: str
    aggregate_sha256: str
    files: dict[str, str]


class FixtureLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    fixtures: dict[str, FixtureLockEntry]


def _fixture_entry(fixtures_root: Path, sample_dir: str) -> FixtureLockEntry:
    relative = Path(sample_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise AaError(f"unsafe fixture sample_dir: {sample_dir!r}")
    sample_root = fixtures_root / relative
    if not sample_root.is_dir():
        raise AaError(f"golden sample not found: {sample_root}")
    files: dict[str, str] = {}
    for path in sorted(sample_root.rglob("*")):
        if path.is_symlink():
            raise AaError(f"fixture contains symlink: {path}")
        if path.is_file():
            rel = path.relative_to(sample_root).as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical = "\n".join(f"{name}:{digest}" for name, digest in sorted(files.items()))
    return FixtureLockEntry(
        sample_dir=relative.as_posix(),
        aggregate_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        files=files,
    )


def write_fixture_lock(fixtures_root: Path, fixtures: dict[str, str]) -> Path:
    """Capture explicit fixture identities for trusted fixture maintenance."""
    lock = FixtureLock(
        fixtures={
            fixture_id: _fixture_entry(fixtures_root, sample_dir)
            for fixture_id, sample_dir in sorted(fixtures.items())
        }
    )
    path = fixtures_root / "fixture-lock.json"
    path.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def resolve_locked_fixture(fixtures_root: Path, fixture_id: str) -> Path:
    lock_path = fixtures_root / "fixture-lock.json"
    try:
        lock = FixtureLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        raise AaError(f"invalid fixture lock {lock_path}: {err}") from err
    expected = lock.fixtures.get(fixture_id)
    if expected is None:
        raise AaError(f"fixture_id not found in fixture lock: {fixture_id}")
    current = _fixture_entry(fixtures_root, expected.sample_dir)
    if current != expected:
        changed = sorted(set(current.files) | set(expected.files))
        changed = [name for name in changed if current.files.get(name) != expected.files.get(name)]
        suffix = f" ({', '.join(changed[:5])})" if changed else ""
        raise AaError(f"fixture lock mismatch for {fixture_id}{suffix}")
    return fixtures_root / expected.sample_dir


def _deep_merge_resets(parent: FixtureResets, child: FixtureResets) -> FixtureResets:
    return FixtureResets(
        workflow_state={**parent.workflow_state, **child.workflow_state},
        qa_yaml={**parent.qa_yaml, **child.qa_yaml},
    )


def _tier_file(tiers_dir: Path, tier_name: str) -> Path:
    by_name = tiers_dir / f"{tier_name}.yaml"
    if by_name.is_file():
        return by_name
    for path in sorted(tiers_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and raw.get("name") == tier_name:
            return path
    raise AaError(f"fixture tier not found: {tier_name} (looked in {tiers_dir})")


def load_tier(fixtures_root: Path, tier_name: str, *, _seen: set[str] | None = None) -> TierManifest:
    """Load and expand a tier manifest, merging ``extends`` parents."""
    seen = _seen or set()
    if tier_name in seen:
        raise AaError(f"fixture tier extends cycle involving {tier_name!r}")
    seen.add(tier_name)
    tiers_dir = fixtures_root / "tiers"
    path = _tier_file(tiers_dir, tier_name)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AaError(f"invalid fixture tier manifest: {path}")
    child = TierManifest.model_validate({**raw, "name": raw.get("name") or tier_name})
    if not child.extends:
        return child
    parent = load_tier(fixtures_root, child.extends, _seen=seen)
    return TierManifest(
        name=child.name,
        extends=child.extends,
        description=child.description or parent.description,
        paths=list(dict.fromkeys([*parent.paths, *child.paths])),
        resets=_deep_merge_resets(parent.resets, child.resets),
        source_prefix=child.source_prefix or parent.source_prefix,
    )


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor: dict[str, Any] = target
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _split_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    change_paths: list[str] = []
    test_paths: list[str] = []
    for rel in paths:
        if rel.startswith("tests/"):
            test_paths.append(rel)
        else:
            change_paths.append(rel)
    return change_paths, test_paths


def _copy_rel(src_root: Path, dest_root: Path, rel: str) -> None:
    src = src_root / rel
    if not src.exists():
        raise AaError(f"fixture path missing: {src}")
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)


def _apply_workflow_state_resets(change_dir: Path, resets: dict[str, Any]) -> None:
    if not resets:
        # Still re-hash if a state file was copied from archive with stale integrity.
        if (change_dir / "workflow-state.yaml").exists():
            state = read_state_lenient(change_dir)
            write_state(change_dir, state)
        return
    state = read_state_lenient(change_dir)
    dumped = state.model_dump(mode="json")
    for key, value in resets.items():
        _set_dotted(dumped, key, value)
    from assurance_agent.artifacts.models import WorkflowState

    write_state(change_dir, WorkflowState.model_validate(dumped))


def _apply_qa_yaml_resets(change_dir: Path, resets: dict[str, Any]) -> None:
    if not resets:
        return
    qa_path = change_dir / ".qa.yaml"
    raw: dict[str, Any] = {}
    if qa_path.exists():
        loaded = yaml.safe_load(qa_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = loaded
    for key, value in resets.items():
        _set_dotted(raw, key, value)
    qa_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def seed_change(
    *,
    sut_sandbox: Path,
    change_id: str,
    tier_name: str,
    fixtures_root: Path,
    fixture_id: str,
) -> None:
    """Reset a sandbox change directory from a golden fixture tier."""
    assert_change_id_safe(change_id)
    tier = load_tier(fixtures_root, tier_name)
    sample_root = resolve_locked_fixture(fixtures_root, fixture_id)

    change_dir = sut_sandbox / "qa" / "changes" / change_id
    staging = sut_sandbox / "qa" / "changes" / f".seed-staging-{change_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    change_paths, test_paths = _split_paths(tier.paths)
    # Always seed the whole sample tree when paths empty (full snapshot).
    if not tier.paths:
        for entry in sample_root.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(sample_root).as_posix()
                if rel.startswith("tests/"):
                    test_paths.append(rel)
                else:
                    change_paths.append(rel)

    for rel in change_paths:
        _copy_rel(sample_root, staging, rel)
    for rel in test_paths:
        _copy_rel(sample_root, sut_sandbox, rel)

    if change_dir.exists():
        shutil.rmtree(change_dir)
    staging.rename(change_dir)

    _apply_workflow_state_resets(change_dir, tier.resets.workflow_state)
    _apply_qa_yaml_resets(change_dir, tier.resets.qa_yaml)
