"""D15 evidence paths: pinned physical identity, aliases, and ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import StrictStr, model_validator

from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.contracts import ContractError, ResourcePath
from assurance_agent.workflow.graph.workspace import (
    WriteSet,
    _assert_safe_prefix,
    _physical_for,
    _resolutions,
)

EvidencePathErrorCode = Literal[
    "missing_pinned_roots",
    "base_tree_roots_mismatch",
    "path_traversal",
    "absolute_path",
    "another_change",
    "ambiguous_containment",
    "unowned_path",
    "unknown_root",
    "invalid_logical_path",
    "unsafe_root_prefix",
]

OwnershipName = Literal["current_change", "repo", "project"]

_OWNERSHIP_PRECEDENCE: tuple[OwnershipName, ...] = ("current_change", "repo", "project")
_CHANGE_FAMILY_PREFIXES = ("qa/changes/", "qa/archive/")


class EvidencePathError(AaError):
    """Fail-closed evidence path resolution / pinned-root validation error."""

    def __init__(self, code: EvidencePathErrorCode, message: str) -> None:
        self.code: EvidencePathErrorCode = code
        super().__init__(f"{code}: {message}")


class ResolvedEvidencePath(StrictWireModel):
    physical_relpath: StrictStr
    repo_relpath: str | None
    ownership: OwnershipName
    logical_aliases: list[StrictStr]

    @model_validator(mode="after")
    def validate_resolved(self) -> ResolvedEvidencePath:
        _validate_relpath(self.physical_relpath, label="physical_relpath")
        if self.repo_relpath is not None:
            _validate_relpath(self.repo_relpath, label="repo_relpath")
        if not self.logical_aliases:
            raise ValueError("logical_aliases must be non-empty")
        if list(self.logical_aliases) != sorted(self.logical_aliases):
            raise ValueError("logical_aliases must be canonically sorted")
        if len(set(self.logical_aliases)) != len(self.logical_aliases):
            raise ValueError("logical_aliases must be unique")
        return self


def resolve_evidence_path(
    *,
    logical_path: str,
    tree_roots: Mapping[str, str],
    current_change_repo_path: str,
) -> ResolvedEvidencePath:
    """Resolve all aliases and classify one pinned physical path.

    Pure resolver: never opens the filesystem or follows symlinks. Physical
    identity is lexical against the pinned root map only.
    """
    roots = _normalize_tree_roots(tree_roots)
    change_repo = _normalize_change_repo_path(current_change_repo_path)
    try:
        logical = ResourcePath.parse(logical_path)
    except ContractError as exc:
        raise _path_error_from_message(str(exc)) from exc
    if logical.root == "global":
        raise EvidencePathError("unknown_root", f"global root is not an evidence path: {logical_path}")
    if logical.root not in roots:
        raise EvidencePathError("unknown_root", f"logical root not in pinned map: {logical.root}")
    try:
        physical = _physical_for(roots, logical)
    except Exception as exc:  # WorkspaceError for unsafe prefix / unknown root
        raise EvidencePathError("unsafe_root_prefix", str(exc)) from exc
    _validate_relpath(physical, label="physical_relpath")

    resolutions = _resolutions(roots, physical)
    if not resolutions:
        raise EvidencePathError("unowned_path", f"path resolves outside every logical root: {physical}")
    aliases = sorted({f"{name}:{rel}" for name, rel in resolutions})
    # Lexical identity: every alias must round-trip to the same physical path.
    for alias in aliases:
        alias_logical = ResourcePath.parse(alias)
        alias_physical = _physical_for(roots, alias_logical)
        if alias_physical != physical:
            raise EvidencePathError(
                "ambiguous_containment",
                f"alias {alias} maps to {alias_physical}, not {physical}",
            )

    if _is_another_change(physical, change_repo, roots):
        raise EvidencePathError(
            "another_change",
            f"path {physical} is under another change, not {change_repo}",
        )

    repo_relpath = next((rel for name, rel in resolutions if name == "repo"), None)
    ownership = _assign_ownership(
        physical,
        {
            "current_change": change_repo,
            "repo": roots.get("repo", ""),
            "project": roots.get("project", ""),
        },
    )
    return ResolvedEvidencePath(
        physical_relpath=physical,
        repo_relpath=repo_relpath,
        ownership=ownership,
        logical_aliases=aliases,
    )


def pinned_write_set_roots(write_set: WriteSet) -> Mapping[str, str]:
    """Return write-set-bound roots or fail closed for current evidence validation."""
    roots = write_set.base_tree_roots
    if roots is None or not roots:
        raise EvidencePathError(
            "missing_pinned_roots",
            f"write set {write_set.write_set_id} has no pinned base_tree_roots",
        )
    return dict(sorted(roots.items()))


def verify_write_set_base_tree_roots(
    write_set: WriteSet,
    tree_roots: Mapping[str, str],
) -> Mapping[str, str]:
    """Require pinned roots and exact agreement with ``base_tree_id`` roots."""
    pinned = pinned_write_set_roots(write_set)
    expected = dict(sorted(_normalize_tree_roots(tree_roots).items()))
    if pinned != expected:
        raise EvidencePathError(
            "base_tree_roots_mismatch",
            f"write set {write_set.write_set_id} base_tree_roots disagree with base_tree_id",
        )
    return pinned


def _normalize_tree_roots(tree_roots: Mapping[str, str]) -> dict[str, str]:
    if not tree_roots:
        raise EvidencePathError("missing_pinned_roots", "pinned tree root map is missing or empty")
    normalized: dict[str, str] = {}
    for name, prefix in tree_roots.items():
        if not isinstance(name, str) or not name:
            raise EvidencePathError("unsafe_root_prefix", f"invalid tree root name: {name!r}")
        try:
            normalized[name] = _assert_safe_prefix(prefix)
        except Exception as exc:
            message = str(exc)
            if isinstance(prefix, str) and prefix.startswith("/"):
                raise EvidencePathError("absolute_path", message) from exc
            if isinstance(prefix, str) and (".." in prefix.split("/") or prefix.startswith("\\")):
                raise EvidencePathError("path_traversal", message) from exc
            raise EvidencePathError("unsafe_root_prefix", message) from exc
    return dict(sorted(normalized.items()))


def _normalize_change_repo_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidencePathError("invalid_logical_path", "current_change_repo_path must be non-empty")
    if value.startswith("/"):
        raise EvidencePathError("absolute_path", f"current_change_repo_path is absolute: {value}")
    try:
        return _assert_safe_prefix(value)
    except Exception as exc:
        message = str(exc)
        if ".." in value.split("/"):
            raise EvidencePathError("path_traversal", message) from exc
        raise EvidencePathError("unsafe_root_prefix", message) from exc


def _validate_relpath(value: str, *, label: str) -> None:
    if value.startswith("/"):
        raise EvidencePathError("absolute_path", f"{label} must be a normalized relative POSIX path")
    if not value or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        code: EvidencePathErrorCode = "path_traversal" if ".." in value.split("/") else "invalid_logical_path"
        raise EvidencePathError(code, f"{label} must be a normalized relative POSIX path")


def _path_error_from_message(message: str) -> EvidencePathError:
    lowered = message.lower()
    if ".." in message or "unsafe resource pattern" in lowered:
        return EvidencePathError("path_traversal", message)
    if message.startswith("/") or "absolute" in lowered:
        return EvidencePathError("absolute_path", message)
    if "invalid resource root" in lowered:
        return EvidencePathError("unknown_root", message)
    return EvidencePathError("invalid_logical_path", message)


def _contained(prefix: str, path: str) -> bool:
    if not prefix:
        return False
    if prefix == ".":
        return True
    return path == prefix or path.startswith(f"{prefix}/")


def _prefix_specificity(prefix: str) -> int:
    if not prefix or prefix == ".":
        return 0
    return len(prefix.split("/"))


def _assign_ownership(physical: str, scopes: Mapping[str, str]) -> OwnershipName:
    """Assign ownership by nested physical containment with fixed precedence.

    Longest containing prefix wins. When several ownership classes share that
    same prefix string, ``current_change > repo > project`` breaks the tie.
    When a higher-precedence ancestor conflicts with a more-specific nested
    descendant (non-nested / precedence disagreement), fail closed.
    """
    containing: list[tuple[OwnershipName, str, int]] = []
    for name in _OWNERSHIP_PRECEDENCE:
        raw = scopes.get(name, "")
        if not raw:
            continue
        try:
            prefix = _assert_safe_prefix(raw) if raw != "." else "."
        except Exception:
            continue
        if _contained(prefix, physical):
            containing.append((name, prefix, _prefix_specificity(prefix)))
    if not containing:
        raise EvidencePathError("unowned_path", f"no unique ownership for physical path: {physical}")

    max_spec = max(item[2] for item in containing)
    top = [(name, prefix) for name, prefix, spec in containing if spec == max_spec]
    top_prefixes = {prefix for _name, prefix in top}
    if len(top_prefixes) > 1:
        raise EvidencePathError(
            "ambiguous_containment",
            f"non-nested ownership prefixes for {physical}: {sorted(top_prefixes)}",
        )

    top_names = {name for name, _prefix in top}
    specific_owner: OwnershipName | None = next(
        (name for name in _OWNERSHIP_PRECEDENCE if name in top_names),
        None,
    )
    if specific_owner is None:
        raise EvidencePathError("unowned_path", f"no unique ownership for physical path: {physical}")
    # If a higher-precedence scope contains the path but is a strict ancestor of
    # the most-specific prefix, nesting and precedence disagree → ambiguous.
    specific_prefix = next(prefix for _name, prefix in top)
    for name, prefix, spec in containing:
        if name == specific_owner:
            continue
        precedence_rank = _OWNERSHIP_PRECEDENCE.index(name)
        specific_rank = _OWNERSHIP_PRECEDENCE.index(specific_owner)
        if precedence_rank < specific_rank and spec < max_spec and _contained(prefix, specific_prefix):
            raise EvidencePathError(
                "ambiguous_containment",
                f"ownership precedence {name} conflicts with nested {specific_owner} for {physical}",
            )
    return specific_owner


def _is_another_change(physical: str, change_repo: str, roots: Mapping[str, str]) -> bool:
    if _contained(change_repo, physical):
        return False
    change_prefix = roots.get("change")
    if change_prefix and change_prefix != "." and _contained(change_prefix, physical):
        return True
    for family in _CHANGE_FAMILY_PREFIXES:
        if physical == family.rstrip("/") or physical.startswith(family):
            return True
    return False


__all__ = [
    "EvidencePathError",
    "EvidencePathErrorCode",
    "ResolvedEvidencePath",
    "pinned_write_set_roots",
    "resolve_evidence_path",
    "verify_write_set_base_tree_roots",
]
