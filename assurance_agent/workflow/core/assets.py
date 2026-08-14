"""Sync packaged skills / OpenCode assets into a target project.

Reads packaged resources only via assurance_agent.resources (no __file__ paths),
writes into <project>/skills/ and <project>/.opencode/. Content-hash based:
reports created / updated / unchanged so callers can print idempotent summaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from assurance_agent import resources
from assurance_agent.config import CONFIG_RELPATH
from assurance_agent.exceptions import AaError

PLUGIN_ENTRY = "./.opencode/plugins/aa.mjs"
OMO_CONFIG_RELPATH = Path(".opencode/oh-my-openagent.json")
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.DOTALL)


class SkillSyncIntegrityError(AaError):
    """A runtime skill copy differs from the packaged source of truth."""


class SyncResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class OpenCodeInitResult(BaseModel):
    opencode_json_created: bool
    skills: SyncResult
    opencode: SyncResult


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while True:
        if (cur / CONFIG_RELPATH).is_file() or (cur / "qa").is_dir():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _walk_resource_files(*rel: str) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)
        except (NotADirectoryError, ValueError):
            out.append(child)
        else:
            out.extend(_walk_resource_files(*child))
    return out


def _sync(
    resource_rel: tuple[str, ...],
    dest_root: Path,
    report_prefix: str,
    result: SyncResult,
    dry_run: bool,
    *,
    containment_root: Path,
) -> None:
    _reject_symlinks_under(dest_root, containment_root=containment_root)
    for parts in _walk_resource_files(*resource_rel):
        rel_under = Path(*parts[len(resource_rel) :])
        report = (Path(report_prefix) / rel_under).as_posix()
        content = resources.read_text(*parts)
        dest = dest_root / rel_under
        _assert_safe_managed_path(containment_root, dest)
        exists_as_file = dest.is_file()
        if exists_as_file and dest.read_text(encoding="utf-8") == content:
            result.unchanged.append(report)
            continue
        status_updated = exists_as_file
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        (result.updated if status_updated else result.created).append(report)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _assert_safe_managed_path(containment_root: Path, target: Path) -> None:
    """Reject traversal and every existing symlink from the boundary to target."""

    boundary = _absolute_without_resolving(containment_root)
    candidate = _absolute_without_resolving(target)
    try:
        relative = candidate.relative_to(boundary)
    except ValueError as exc:
        raise SkillSyncIntegrityError(
            f"managed skill path {candidate} escapes containment root {boundary}"
        ) from exc

    current = boundary
    paths = [current]
    for part in relative.parts:
        current = current / part
        paths.append(current)
    for path in paths:
        if path.is_symlink():
            label = path.relative_to(boundary).as_posix() or "."
            raise SkillSyncIntegrityError(f"managed skill path {label}: symbolic link is not allowed")

    # This is intentionally a post-symlink check: resolve is used only to
    # compare locations, never as the path subsequently read or written.
    resolved_boundary = boundary.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_boundary):
        raise SkillSyncIntegrityError(
            f"managed skill path {candidate} resolves outside containment root {boundary}"
        )


def _reject_symlinks_under(managed_root: Path, *, containment_root: Path) -> None:
    """Walk a managed tree without following links and reject any link found."""

    _assert_safe_managed_path(containment_root, managed_root)
    if not managed_root.exists():
        return
    if not managed_root.is_dir():
        raise SkillSyncIntegrityError(f"managed skill root {managed_root} is not a directory")

    pending = [managed_root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise SkillSyncIntegrityError(
                f"managed skill directory {directory} is unreadable: {exc}"
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                relative = entry.relative_to(containment_root).as_posix()
                raise SkillSyncIntegrityError(f"managed skill path {relative}: symbolic link is not allowed")
            try:
                if entry.is_dir():
                    pending.append(entry)
            except OSError as exc:
                raise SkillSyncIntegrityError(f"managed skill path {entry} is unreadable: {exc}") from exc


def sync_skills(project_root: Path, dry_run: bool = False) -> SyncResult:
    destination = project_root / "skills"
    managed_names = _packaged_aa_skill_names()
    result = SyncResult()
    obsolete_entries = _prune_obsolete_namespaced_skills(
        destination,
        managed_names,
        result,
        dry_run=dry_run,
        report_prefix="skills",
    )
    _validate_namespaced_runtime_layout(
        destination,
        managed_names,
        ignored_obsolete_entries=obsolete_entries,
    )
    _sync(
        ("skills",),
        destination,
        "skills",
        result,
        dry_run,
        containment_root=project_root,
    )
    return result


def _opencode_user_config_root() -> Path:
    configured = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if configured:
        config_root = Path(configured).expanduser()
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        config_base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
        config_root = config_base / "opencode"
    return _absolute_without_resolving(config_root)


def opencode_user_skills_root() -> Path:
    """Return the canonical OpenCode CLI user-skill directory used by OMO."""

    return _opencode_user_config_root() / "skills"


def opencode_user_agents_root() -> Path:
    """Return the canonical OpenCode CLI user-agent directory."""

    return _opencode_user_config_root() / "agents"


def _packaged_aa_skill_names() -> dict[str, str]:
    return {name.casefold(): name for name in resources.iter_children("skills") if name.startswith("aa-")}


def _validate_namespaced_runtime_layout(
    skills_root: Path,
    managed_names: dict[str, str],
    *,
    ignored_obsolete_entries: frozenset[str] = frozenset(),
) -> None:
    """Validate the runtime namespace without following any symbolic link.

    A link whose ordinary filename is outside ``aa-*`` can still declare an
    ``aa-*`` frontmatter name at its target.  OMO follows that link during
    discovery, so ignoring it would let an unverified skill shadow a managed
    one.  Reject links fail-closed while leaving both the link and its target
    untouched for their owner to resolve.
    """

    _assert_safe_managed_path(skills_root.parent, skills_root)
    if not skills_root.exists():
        return
    if not skills_root.is_dir():
        raise SkillSyncIntegrityError(f"managed skill root {skills_root} is not a directory")
    try:
        entries = list(skills_root.iterdir())
    except OSError as exc:
        raise SkillSyncIntegrityError(f"managed skill directory {skills_root} is unreadable: {exc}") from exc

    for entry in entries:
        entry_name = entry.stem if entry.suffix.casefold() == ".md" else entry.name
        folded_name = entry_name.casefold()
        canonical_name = managed_names.get(folded_name)
        if entry.is_symlink():
            raise SkillSyncIntegrityError(f"managed skill path {entry.name}: symbolic link is not allowed")
        if folded_name.startswith("aa-") and canonical_name is None:
            if entry.name in ignored_obsolete_entries:
                continue
            raise SkillSyncIntegrityError(
                f"managed skill path {entry.name}: unexpected AA skill namespace entry"
            )
        if canonical_name is not None and entry_name != canonical_name:
            raise SkillSyncIntegrityError(
                f"managed skill path {entry.name}: shadows managed skill {canonical_name}"
            )

    for canonical_name in managed_names.values():
        _reject_symlinks_under(
            skills_root / canonical_name,
            containment_root=skills_root,
        )

    for candidate in _runtime_skill_candidates(skills_root):
        relative = candidate.relative_to(skills_root)
        if relative.parts and relative.parts[0] in ignored_obsolete_entries:
            continue
        declared_name = _runtime_skill_name(candidate)
        if declared_name is None:
            continue
        folded_name = declared_name.casefold()
        canonical_name = managed_names.get(folded_name)
        if canonical_name is not None:
            canonical_entrypoint = Path(canonical_name) / "SKILL.md"
            if relative != canonical_entrypoint:
                raise SkillSyncIntegrityError(f"{relative.as_posix()}: shadows managed skill {declared_name}")
        elif folded_name.startswith("aa-"):
            raise SkillSyncIntegrityError(f"{relative.as_posix()}: unexpected AA skill {declared_name}")


def _prune_obsolete_namespaced_skills(
    skills_root: Path,
    managed_names: dict[str, str],
    result: SyncResult,
    *,
    dry_run: bool,
    report_prefix: str,
) -> frozenset[str]:
    """Remove only ordinary top-level entries in AA's reserved namespace."""

    _assert_safe_managed_path(skills_root.parent, skills_root)
    if not skills_root.exists():
        return frozenset()
    if not skills_root.is_dir():
        raise SkillSyncIntegrityError(f"managed skill root {skills_root} is not a directory")
    try:
        entries = list(skills_root.iterdir())
    except OSError as exc:
        raise SkillSyncIntegrityError(f"managed skill directory {skills_root} is unreadable: {exc}") from exc

    obsolete_entries: set[str] = set()
    for entry in entries:
        entry_name = entry.stem if entry.suffix.casefold() == ".md" else entry.name
        folded_name = entry_name.casefold()
        if not folded_name.startswith("aa-") or folded_name in managed_names:
            continue
        if entry.is_symlink():
            raise SkillSyncIntegrityError(f"managed skill path {entry.name}: symbolic link is not allowed")
        _assert_safe_managed_path(skills_root, entry)
        if entry.is_dir():
            _reject_symlinks_under(entry, containment_root=skills_root)
            if not dry_run:
                shutil.rmtree(entry)
        elif entry.is_file():
            if not dry_run:
                entry.unlink()
        else:
            raise SkillSyncIntegrityError(f"managed skill path {entry.name}: unsupported AA namespace entry")
        obsolete_entries.add(entry.name)
        result.removed.append(f"{report_prefix}/{entry.name}")
    return frozenset(obsolete_entries)


def sync_opencode_user_skills(
    skills_root: Path | None = None,
    dry_run: bool = False,
) -> SyncResult:
    """Sync namespaced AA skills to OMO's user-level runtime directory."""

    destination = skills_root or opencode_user_skills_root()
    managed_names = _packaged_aa_skill_names()
    result = SyncResult()
    obsolete_entries = _prune_obsolete_namespaced_skills(
        destination,
        managed_names,
        result,
        dry_run=dry_run,
        report_prefix="opencode-user-skills",
    )
    _validate_namespaced_runtime_layout(
        destination,
        managed_names,
        ignored_obsolete_entries=obsolete_entries,
    )
    for name in sorted(managed_names.values()):
        _sync(
            ("skills", name),
            destination / name,
            f"opencode-user-skills/{name}",
            result,
            dry_run,
            containment_root=destination,
        )
    return result


def _packaged_aa_agent_files() -> dict[str, str]:
    return {
        Path(filename).stem.casefold(): filename
        for filename in resources.iter_children("opencode", "agents")
        if filename.casefold().startswith("aa-") and filename.casefold().endswith(".md")
    }


def _agent_entry_names(entry: Path) -> tuple[str, str | None]:
    filename_name = entry.stem if entry.suffix.casefold() == ".md" else entry.name
    declared_name = _runtime_skill_name(entry) if entry.is_file() else None
    return filename_name, declared_name


def _prune_obsolete_namespaced_agents(
    agents_root: Path,
    managed_files: dict[str, str],
    result: SyncResult,
    *,
    dry_run: bool,
) -> None:
    _assert_safe_managed_path(agents_root.parent, agents_root)
    if not agents_root.exists():
        return
    if not agents_root.is_dir():
        raise SkillSyncIntegrityError(f"managed agent root {agents_root} is not a directory")
    _reject_symlinks_under(agents_root, containment_root=agents_root.parent)
    for entry in sorted(agents_root.iterdir(), key=lambda candidate: candidate.name):
        filename_name, declared_name = _agent_entry_names(entry)
        folded_names = {filename_name.casefold()}
        if declared_name is not None:
            folded_names.add(declared_name.casefold())
        if folded_names & managed_files.keys():
            continue
        if not any(name.startswith("aa-") for name in folded_names):
            continue
        _assert_safe_managed_path(agents_root, entry)
        if not dry_run:
            if entry.is_dir():
                shutil.rmtree(entry)
            elif entry.is_file():
                entry.unlink()
            else:
                raise SkillSyncIntegrityError(
                    f"managed agent path {entry.name}: unsupported AA namespace entry"
                )
        result.removed.append(f"opencode-user-agents/{entry.name}")


def _validate_namespaced_agent_layout(
    agents_root: Path,
    managed_files: dict[str, str],
) -> None:
    _assert_safe_managed_path(agents_root.parent, agents_root)
    if not agents_root.exists():
        return
    if not agents_root.is_dir():
        raise SkillSyncIntegrityError(f"managed agent root {agents_root} is not a directory")
    _reject_symlinks_under(agents_root, containment_root=agents_root.parent)
    for entry in sorted(agents_root.iterdir(), key=lambda candidate: candidate.name):
        filename_name, declared_name = _agent_entry_names(entry)
        filename_match = managed_files.get(filename_name.casefold())
        declared_match = managed_files.get(declared_name.casefold()) if declared_name is not None else None
        expected = filename_match or declared_match
        if expected is None:
            continue
        expected_name = Path(expected).stem
        if entry.name != expected or declared_name not in (None, expected_name):
            raise SkillSyncIntegrityError(
                f"managed agent path {entry.name}: shadows managed agent {expected_name}"
            )


def sync_opencode_user_agents(
    agents_root: Path | None = None,
    dry_run: bool = False,
) -> SyncResult:
    """Sync packaged AA agents to OpenCode's user-level runtime directory."""

    destination = agents_root or opencode_user_agents_root()
    managed_files = _packaged_aa_agent_files()
    result = SyncResult()
    _prune_obsolete_namespaced_agents(
        destination,
        managed_files,
        result,
        dry_run=dry_run,
    )
    _validate_namespaced_agent_layout(destination, managed_files)
    _sync(
        ("opencode", "agents"),
        destination,
        "opencode-user-agents",
        result,
        dry_run,
        containment_root=destination.parent,
    )
    return result


def verify_packaged_agents(agents_root: Path) -> int:
    """Fail closed unless the managed user-agent namespace matches packaged bytes."""

    managed_files = _packaged_aa_agent_files()
    _validate_namespaced_agent_layout(agents_root, managed_files)
    mismatches: list[str] = []
    verified = 0
    for filename in sorted(managed_files.values()):
        expected = resources.read_text("opencode", "agents", filename).encode("utf-8")
        target = agents_root / filename
        if not target.is_file():
            mismatches.append(f"{filename}: missing")
            continue
        try:
            actual = target.read_bytes()
        except OSError as exc:
            mismatches.append(f"{filename}: unreadable ({exc})")
            continue
        if actual != expected:
            mismatches.append(f"{filename}: sha256 mismatch")
            continue
        verified += 1
    if mismatches:
        raise SkillSyncIntegrityError(
            f"agent sync integrity check failed at {agents_root}: " + "; ".join(mismatches[:10])
        )
    return verified


def _runtime_skill_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FRONTMATTER_RE.match(text)
    if match is not None:
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            frontmatter = None
        if isinstance(frontmatter, dict):
            declared = frontmatter.get("name")
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
    return path.parent.name if path.name == "SKILL.md" else path.stem


def _runtime_skill_candidates(skills_root: Path) -> list[Path]:
    if not skills_root.is_dir():
        return []
    candidates: set[Path] = set()

    def discover(directory: Path, depth: int) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            return

        directories: list[Path] = []
        files: list[Path] = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                continue
            try:
                (directories if entry.is_dir() else files).append(entry)
            except OSError:
                continue

        for entry in directories:
            skill_md = entry / "SKILL.md"
            named_md = entry / f"{entry.name}.md"
            if skill_md.is_file():
                candidates.add(skill_md)
            elif named_md.is_file():
                candidates.add(named_md)
            elif depth < 2:
                discover(entry, depth + 1)

        for entry in files:
            if entry.suffix.lower() == ".md" and entry.is_file():
                candidates.add(entry)

    discover(skills_root, 0)
    return sorted(candidates, key=lambda candidate: candidate.as_posix())


def verify_packaged_skills(skills_root: Path, *, namespaced_only: bool = False) -> int:
    """Fail closed unless every managed skill file matches packaged bytes."""

    managed_names = _packaged_aa_skill_names()
    if namespaced_only:
        _validate_namespaced_runtime_layout(skills_root, managed_names)
    else:
        _reject_symlinks_under(skills_root, containment_root=skills_root.parent)
        _validate_namespaced_runtime_layout(skills_root, managed_names)
    mismatches: list[str] = []
    verified = 0
    expected_files: set[Path] = set()
    for parts in sorted(_walk_resource_files("skills")):
        rel = Path(*parts[1:])
        if namespaced_only and not rel.parts[0].startswith("aa-"):
            continue
        expected_files.add(rel)
        expected = resources.read_text(*parts).encode("utf-8")
        target = skills_root / rel
        if not target.is_file():
            mismatches.append(f"{rel.as_posix()}: missing")
            continue
        try:
            actual = target.read_bytes()
        except OSError as exc:
            mismatches.append(f"{rel.as_posix()}: unreadable ({exc})")
            continue
        expected_digest = hashlib.sha256(expected).hexdigest()
        actual_digest = hashlib.sha256(actual).hexdigest()
        if actual_digest != expected_digest:
            mismatches.append(
                f"{rel.as_posix()}: sha256 mismatch (expected {expected_digest}, actual {actual_digest})"
            )
            continue
        verified += 1

    for candidate in _runtime_skill_candidates(skills_root):
        rel = candidate.relative_to(skills_root)
        if rel in expected_files:
            continue
        declared_name = _runtime_skill_name(candidate)
        if declared_name is None:
            continue
        folded_name = declared_name.casefold()
        if folded_name in managed_names:
            mismatches.append(f"{rel.as_posix()}: shadows managed skill {declared_name}")
        elif folded_name.startswith("aa-"):
            mismatches.append(f"{rel.as_posix()}: unexpected AA skill {declared_name}")

    if mismatches:
        detail = "; ".join(mismatches[:10])
        if len(mismatches) > 10:
            detail += f"; ... and {len(mismatches) - 10} more"
        raise SkillSyncIntegrityError(f"skill sync integrity check failed at {skills_root}: {detail}")
    return verified


def sync_opencode(project_root: Path, dry_run: bool = False) -> SyncResult:
    destination = project_root / ".opencode" / "skills"
    managed_names = _packaged_aa_skill_names()
    result = SyncResult()
    # Only AA-owned subtrees are managed.  OpenCode also owns package-manager
    # state below .opencode (notably node_modules/.bin symlinks), which must not
    # be traversed or rejected by the asset synchronizer.
    _assert_safe_managed_path(project_root, project_root / ".opencode")
    obsolete_entries = _prune_obsolete_namespaced_skills(
        destination,
        managed_names,
        result,
        dry_run=dry_run,
        report_prefix=".opencode/skills",
    )
    _validate_namespaced_runtime_layout(
        destination,
        managed_names,
        ignored_obsolete_entries=obsolete_entries,
    )
    # OMO replaces OpenCode's native skill tool and discovers project-scoped
    # skills from .opencode/skills.  Keep a runtime mirror there instead of
    # relying only on the AA plugin's skills.paths registration.
    _sync(
        ("skills",),
        destination,
        ".opencode/skills",
        result,
        dry_run,
        containment_root=project_root,
    )
    for sub in ("agents", "tools", "plugins"):
        if resources.exists("opencode", sub):
            _sync(
                ("opencode", sub),
                project_root / ".opencode" / sub,
                f".opencode/{sub}",
                result,
                dry_run,
                containment_root=project_root,
            )
    _disable_incompatible_opencode_tools(project_root, result, dry_run=dry_run)
    _disable_incompatible_omo_tools(project_root, result, dry_run=dry_run)
    return result


def _disable_incompatible_opencode_tools(
    project_root: Path,
    result: SyncResult,
    *,
    dry_run: bool,
) -> None:
    """Hide incompatible built-in tools without replacing project config."""

    target = project_root / "opencode.json"
    _assert_safe_managed_path(project_root, target)
    if not target.is_file():
        return
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillSyncIntegrityError(f"invalid JSON in {target}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SkillSyncIntegrityError(f"expected a JSON object in {target}")
    tools = loaded.get("tools", {})
    if not isinstance(tools, dict) or any(not isinstance(key, str) for key in tools):
        raise SkillSyncIntegrityError(f"expected tools to be a JSON object in {target}")
    incompatible = ("apply_patch", "webfetch", "websearch", "websearch_web_search_exa")
    if all(tools.get(name) is False for name in incompatible):
        result.unchanged.append("opencode.json")
        return
    loaded["tools"] = {**tools, **dict.fromkeys(incompatible, False)}
    if not dry_run:
        target.write_text(json.dumps(loaded, indent=2) + "\n", encoding="utf-8")
    result.updated.append("opencode.json")


def _disable_incompatible_omo_tools(
    project_root: Path,
    result: SyncResult,
    *,
    dry_run: bool,
) -> None:
    """Hide OMO tools whose schemas are incompatible with supported providers."""

    target = project_root / OMO_CONFIG_RELPATH
    _assert_safe_managed_path(project_root, target)
    existed = target.is_file()
    config: dict = {}
    if existed:
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillSyncIntegrityError(f"invalid JSON in {target}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SkillSyncIntegrityError(f"expected a JSON object in {target}")
        config = loaded
    disabled = config.get("disabled_tools", [])
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise SkillSyncIntegrityError(f"expected disabled_tools to be a string list in {target}")
    incompatible = ("apply_patch", "webfetch", "websearch", "websearch_web_search_exa")
    missing = [name for name in incompatible if name not in disabled]
    if not missing:
        result.unchanged.append(OMO_CONFIG_RELPATH.as_posix())
        return
    config["disabled_tools"] = [*disabled, *missing]
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (result.updated if existed else result.created).append(OMO_CONFIG_RELPATH.as_posix())


def ensure_opencode_plugin_registration(project_root: Path, *, dry_run: bool = False) -> bool:
    """Ensure the synchronized AA plugin is referenced by project config.

    Returns whether the config is newly created (or replaces unreadable JSON),
    preserving the init command's existing reporting semantics.
    """

    opencode_json = project_root / "opencode.json"
    created = not opencode_json.is_file()
    config: dict = {}
    changed = created
    if not created:
        try:
            config = json.loads(opencode_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
            created = True  # unreadable file is rewritten fresh
            changed = True

    plugins = config.get("plugin")
    if not isinstance(plugins, list):
        plugins = []
        changed = True
    if PLUGIN_ENTRY not in plugins:
        plugins.append(PLUGIN_ENTRY)
        changed = True
    config["plugin"] = plugins
    tools = config.get("tools")
    if not isinstance(tools, dict):
        tools = {}
        changed = True
    for name in ("apply_patch", "webfetch", "websearch", "websearch_web_search_exa"):
        if tools.get(name) is not False:
            tools[name] = False
            changed = True
    config["tools"] = tools
    if changed and not dry_run:
        opencode_json.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return created


def register_opencode(project_root: Path) -> OpenCodeInitResult:
    created = ensure_opencode_plugin_registration(project_root)

    return OpenCodeInitResult(
        opencode_json_created=created,
        skills=sync_skills(project_root),
        opencode=sync_opencode(project_root),
    )
