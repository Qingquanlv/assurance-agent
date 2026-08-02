"""Content-aware worktree manifests, write diffs, and selected-layer write policy.

Porcelain git status remains diagnostic only. Authority is D12 content manifests
plus ``WriteDiffV1`` / ``WritePolicyV1``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError
from assurance_agent.verification.generated_files import get_generated_files_contract

# Unified evidence layout under <attempt_dir>/evidence/
EVIDENCE_SUBDIR = "evidence"
GIT_STATUS_BEFORE = "git-status-before.bin"
GIT_STATUS_AFTER = "git-status-after.bin"
WRITE_MANIFEST_BEFORE = "write-manifest-before.json"
WRITE_MANIFEST_AFTER = "write-manifest-after.json"
WRITE_DIFF_JSON = "write-diff.json"
WRITE_POLICY_JSON = "write-policy.json"

WRITE_POLICY_SCHEMA_VERSION: Final[str] = "write_policy/v1"
MAX_MANIFEST_ENTRIES: Final[int] = 250_000
MAX_MANIFEST_FILE_BYTES: Final[int] = 4 * 1024 * 1024 * 1024

_RUNTIME_METADATA_ALLOWLIST = (
    "qa/.graph-runtime/locks/**",
    "qa/.graph-runtime/publications/**",
)

_LAYER_PRIVATE_ROOT: dict[LayerName, str] = {
    layer: get_generated_files_contract(layer).private_test_root for layer in LAYER_NAMES
}

DEFAULT_RUN_DENYLIST: list[str] = [
    ".aa/memory/**",
    "backend/**",
    "frontend/**",
    "src/**",
    "!src/tests/**",
]

# Legacy TS-aligned allowlists retained for diagnostic comparison / migration tests.
DEFAULT_ALLOWLISTS: dict[str, list[str]] = {
    "workflow_case": [
        "qa/changes/eval-sample-*/**",
        "qa/changes/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
    "workflow_api_codegen": [
        "qa/changes/eval-sample-*/**",
        "qa/changes/**",
        "tests/api",
        "tests/api/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
    "workflow_e2e_codegen": [
        "qa/changes/eval-sample-*/**",
        "qa/changes/**",
        "tests/e2e",
        "tests/e2e/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
    "workflow_fuzz_codegen": [
        "qa/changes/eval-sample-*/**",
        "qa/changes/**",
        "tests/fuzz",
        "tests/fuzz/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
    "workflow_performance_codegen": [
        "qa/changes/eval-sample-*/**",
        "qa/changes/**",
        "tests/perf",
        "tests/perf/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
    "safety_lite": [
        "qa/changes/**",
        "tests/**",
        "eval/out/runs/**",
        *_RUNTIME_METADATA_ALLOWLIST,
    ],
}

DiffReason = Literal[
    "added",
    "deleted",
    "content_changed",
    "mode_changed",
    "kind_changed",
    "symlink_target_changed",
]


class WriteScanError(AaError):
    """Content-manifest / write-policy infrastructure failure."""


class WorktreeManifestEntryV1(StrictWireModel):
    path: str
    kind: Literal["file", "symlink"]
    mode: int
    size: int
    sha256: str | None
    symlink_target: str | None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> WorktreeManifestEntryV1:
        _validate_repo_path(self.path)
        if self.kind == "file":
            if self.sha256 is None or self.symlink_target is not None:
                raise ValueError("file entry requires sha256 and omits symlink_target")
            if (
                len(self.sha256) != len("sha256:") + 64
                or not self.sha256.startswith("sha256:")
                or any(c not in "0123456789abcdef" for c in self.sha256.removeprefix("sha256:"))
            ):
                raise ValueError("file sha256 must be lowercase sha256:<64-hex>")
            if self.size < 0:
                raise ValueError("file size must be non-negative")
        else:
            if self.symlink_target is None or self.sha256 is not None:
                raise ValueError("symlink entry requires symlink_target and omits sha256")
            if self.size != 0:
                raise ValueError("symlink size must be 0")
        return self


class WorktreeManifestV1(StrictWireModel):
    schema_version: Literal["1"]
    entries: list[WorktreeManifestEntryV1]
    total_entries: int
    total_file_bytes: int

    @model_validator(mode="after")
    def validate_aggregates(self) -> WorktreeManifestV1:
        paths = [e.path for e in self.entries]
        if paths != sorted(paths):
            raise ValueError("entries must be sorted by path")
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate manifest paths")
        if self.total_entries != len(self.entries):
            raise ValueError("total_entries mismatch")
        file_bytes = sum(e.size for e in self.entries if e.kind == "file")
        if self.total_file_bytes != file_bytes:
            raise ValueError("total_file_bytes mismatch")
        return self


class WriteDiffEntryV1(StrictWireModel):
    path: str
    reasons: list[DiffReason] = Field(min_length=1)
    before: WorktreeManifestEntryV1 | None
    after: WorktreeManifestEntryV1 | None

    @model_validator(mode="after")
    def validate_entry(self) -> WriteDiffEntryV1:
        _validate_repo_path(self.path)
        if self.before is None and self.after is None:
            raise ValueError("diff entry requires before and/or after")
        if self.reasons != sorted(set(self.reasons), key=_REASON_ORDER.__getitem__):
            raise ValueError("reasons must be unique and canonically ordered")
        if self.before is not None and self.before.path != self.path:
            raise ValueError("before.path must match entry path")
        if self.after is not None and self.after.path != self.path:
            raise ValueError("after.path must match entry path")
        return self


class WriteDiffV1(StrictWireModel):
    schema_version: Literal["1"]
    before_manifest_sha256: str
    after_manifest_sha256: str
    entries: list[WriteDiffEntryV1]

    @model_validator(mode="after")
    def validate_diff(self) -> WriteDiffV1:
        for digest in (self.before_manifest_sha256, self.after_manifest_sha256):
            if (
                len(digest) != len("sha256:") + 64
                or not digest.startswith("sha256:")
                or any(c not in "0123456789abcdef" for c in digest.removeprefix("sha256:"))
            ):
                raise ValueError("manifest digest must be lowercase sha256:<64-hex>")
        paths = [e.path for e in self.entries]
        if paths != sorted(paths):
            raise ValueError("diff entries must be sorted by path")
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate diff paths")
        return self


class WritePolicyV1(StrictWireModel):
    schema_version: Literal["1"]
    write_policy_schema_version: Literal["write_policy/v1"]
    mode: Literal["allowlist", "denylist"]
    patterns: list[str]
    selected_layers: list[LayerName]
    change_repo_path: str | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> WritePolicyV1:
        if self.selected_layers != [layer for layer in LAYER_NAMES if layer in self.selected_layers]:
            raise ValueError("selected_layers must be canonical unique order")
        if len(set(self.selected_layers)) != len(self.selected_layers):
            raise ValueError("selected_layers must be unique")
        if self.patterns != list(self.patterns):
            raise ValueError("patterns must be a list")
        if self.mode == "allowlist" and self.change_repo_path is None and self.selected_layers:
            # codegen-only always binds an active change path; case-only may omit.
            pass
        if self.change_repo_path is not None:
            _validate_repo_path(self.change_repo_path)
        return self


# Backward-compatible dataclass aliases used by older scorers/tests until fully migrated.
@dataclass(frozen=True)
class WritePolicy:
    mode: str
    patterns: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"mode": self.mode, "patterns": list(self.patterns)}


@dataclass(frozen=True)
class WriteScanResult:
    forbidden_write_executed_count: int
    changed_paths: list[str]
    violation_paths: list[str]


_REASON_ORDER: dict[DiffReason, int] = {
    "added": 0,
    "deleted": 1,
    "content_changed": 2,
    "mode_changed": 3,
    "kind_changed": 4,
    "symlink_target_changed": 5,
}


def _validate_repo_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("must be a normalized repository-relative POSIX path")
    return value


def policy_from_dict(data: dict) -> WritePolicy:
    """Tolerant parse of a persisted write-policy.json (legacy scorer path)."""
    patterns = data.get("patterns")
    return WritePolicy(
        mode="allowlist" if data.get("mode") == "allowlist" else "denylist",
        patterns=tuple(p for p in patterns if isinstance(p, str)) if isinstance(patterns, list) else (),
    )


def parse_single_test_type(raw: object) -> str:
    """Deprecated — prefer canonical selected_layers tuples."""
    if isinstance(raw, (list, tuple)):
        types = [str(part).strip() for part in raw if str(part).strip()]
    else:
        types = [t for t in (part.strip() for part in str(raw if raw is not None else "api").split(",")) if t]
    if len(types) != 1:
        raise AaError(f"codegen-only requires exactly one test type, got: {','.join(types) or '(empty)'}")
    return types[0]


def build_write_policy_v1(
    *,
    run_mode: str,
    selected_layers: Sequence[LayerName],
    change_repo_path: str | None,
) -> WritePolicyV1:
    """Construct strict write policy from canonical selection + active change path."""
    layers: tuple[LayerName, ...] = tuple(layer for layer in LAYER_NAMES if layer in set(selected_layers))
    if len(layers) != len(set(selected_layers)):
        raise WriteScanError("selected_layers must be a unique canonical subset")
    if tuple(selected_layers) != layers:
        raise WriteScanError("selected_layers must already be in canonical order")
    selected_list: list[LayerName] = list(layers)

    if run_mode == "codegen-only":
        if not change_repo_path:
            raise WriteScanError("codegen-only write policy requires resolved change_repo_path")
        _validate_repo_path(change_repo_path)
        patterns: list[str] = [
            f"{change_repo_path}/**",
            *_RUNTIME_METADATA_ALLOWLIST,
            "tests/testdata",
            "tests/testdata/**",
        ]
        for layer in layers:
            root = _LAYER_PRIVATE_ROOT[layer]
            patterns.append(root)
            patterns.append(f"{root}/**")
        return WritePolicyV1(
            schema_version="1",
            write_policy_schema_version="write_policy/v1",
            mode="allowlist",
            patterns=patterns,
            selected_layers=selected_list,
            change_repo_path=change_repo_path,
        )
    if run_mode == "case-only":
        if not change_repo_path:
            raise WriteScanError("case-only write policy requires resolved change_repo_path")
        patterns = [
            f"{change_repo_path}/**",
            *_RUNTIME_METADATA_ALLOWLIST,
        ]
        return WritePolicyV1(
            schema_version="1",
            write_policy_schema_version="write_policy/v1",
            mode="allowlist",
            patterns=patterns,
            selected_layers=selected_list,
            change_repo_path=change_repo_path,
        )
    return WritePolicyV1(
        schema_version="1",
        write_policy_schema_version="write_policy/v1",
        mode="denylist",
        patterns=list(DEFAULT_RUN_DENYLIST),
        selected_layers=selected_list,
        change_repo_path=change_repo_path,
    )


def resolve_write_policy(run_mode: str, selected_layers: object) -> WritePolicy:
    """Legacy adapter returning the dataclass shape used by older callers."""
    if isinstance(selected_layers, (list, tuple)):
        layers = tuple(str(part) for part in selected_layers)
    elif selected_layers is None:
        layers = ("api", "e2e")
    else:
        raise AaError(
            "write policy requires a canonical selected_layers sequence; do not pass unresolved suite strings"
        )
    # Legacy codegen path still single-layer until callers migrate to build_write_policy_v1.
    if run_mode == "codegen-only" and len(layers) != 1:
        raise AaError(f"codegen-only requires exactly one test type, got: {','.join(layers) or '(empty)'}")
    typed = tuple(layer for layer in LAYER_NAMES if layer in layers)
    if run_mode == "codegen-only":
        # Temporary wide allowlist for transitional callers that lack change_repo_path.
        root = _LAYER_PRIVATE_ROOT[typed[0]]  # type: ignore[index]
        patterns = (
            "qa/changes/**",
            root,
            f"{root}/**",
            "eval/out/runs/**",
            *_RUNTIME_METADATA_ALLOWLIST,
        )
        return WritePolicy(mode="allowlist", patterns=patterns)
    if run_mode == "case-only":
        return WritePolicy(mode="allowlist", patterns=tuple(DEFAULT_ALLOWLISTS["workflow_case"]))
    return WritePolicy(mode="denylist", patterns=tuple(DEFAULT_RUN_DENYLIST))


def selected_layer_contract_write_claims(selected_layers: Sequence[LayerName]) -> frozenset[str]:
    """Union of private-root / testdata claims for graph-authority parity tests."""
    claims: set[str] = {"tests/testdata", "tests/testdata/**"}
    for layer in selected_layers:
        root = _LAYER_PRIVATE_ROOT[layer]
        claims.add(root)
        claims.add(f"{root}/**")
        claims.add(f"repo:{root}/**")
    return frozenset(claims)


def capture_worktree_manifest(
    project_dir: Path,
    *,
    exclude_dirs: Collection[str] | None = None,
) -> WorktreeManifestV1:
    """Capture a fail-closed leaf manifest using lstat (never follows symlinks)."""
    root = project_dir if project_dir.is_absolute() else project_dir.absolute()
    excluded = {".git", *(exclude_dirs or ())}
    entries: list[WorktreeManifestEntryV1] = []
    total_file_bytes = 0

    def _walk(current: Path, rel_parts: tuple[str, ...]) -> None:
        nonlocal total_file_bytes
        try:
            names = sorted(os.listdir(current))
        except OSError as exc:
            raise WriteScanError(f"failed to list {current}: {exc}") from exc
        for name in names:
            if not rel_parts and name in excluded:
                continue
            child = current / name
            child_rel = "/".join((*rel_parts, name))
            try:
                st = os.lstat(child)
            except OSError as exc:
                raise WriteScanError(f"failed to lstat {child_rel}: {exc}") from exc
            mode = int(st.st_mode)
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                _walk(child, (*rel_parts, name))
                continue
            if stat.S_ISLNK(mode):
                try:
                    target = os.readlink(child)
                except OSError as exc:
                    raise WriteScanError(f"failed to readlink {child_rel}: {exc}") from exc
                # Revalidate identity/kind.
                try:
                    st2 = os.lstat(child)
                except OSError as exc:
                    raise WriteScanError(f"failed to re-lstat {child_rel}: {exc}") from exc
                if not stat.S_ISLNK(st2.st_mode) or int(st2.st_ino) != int(st.st_ino):
                    raise WriteScanError(f"identity/kind race at {child_rel}")
                entries.append(
                    WorktreeManifestEntryV1(
                        path=child_rel,
                        kind="symlink",
                        mode=mode,
                        size=0,
                        sha256=None,
                        symlink_target=target,
                    )
                )
            elif stat.S_ISREG(mode):
                try:
                    data = child.read_bytes()
                except OSError as exc:
                    raise WriteScanError(f"failed to read {child_rel}: {exc}") from exc
                try:
                    st2 = os.lstat(child)
                except OSError as exc:
                    raise WriteScanError(f"failed to re-lstat {child_rel}: {exc}") from exc
                if not stat.S_ISREG(st2.st_mode) or int(st2.st_ino) != int(st.st_ino):
                    raise WriteScanError(f"identity/kind race at {child_rel}")
                if int(st2.st_size) != len(data):
                    raise WriteScanError(f"size race at {child_rel}")
                total_file_bytes += len(data)
                if total_file_bytes > MAX_MANIFEST_FILE_BYTES:
                    raise WriteScanError("worktree manifest exceeds 4 GiB hashed file bytes")
                entries.append(
                    WorktreeManifestEntryV1(
                        path=child_rel,
                        kind="file",
                        mode=mode,
                        size=len(data),
                        sha256=sha256_bytes(data),
                        symlink_target=None,
                    )
                )
            else:
                raise WriteScanError(f"unsupported leaf kind at {child_rel}")
            if len(entries) > MAX_MANIFEST_ENTRIES:
                raise WriteScanError("worktree manifest exceeds 250,000 entries")

    _walk(root, ())
    entries.sort(key=lambda e: e.path)
    return WorktreeManifestV1(
        schema_version="1",
        entries=entries,
        total_entries=len(entries),
        total_file_bytes=total_file_bytes,
    )


def manifest_leaf_paths(manifest: WorktreeManifestV1) -> frozenset[str]:
    return frozenset(entry.path for entry in manifest.entries)


def diff_worktree_manifests(before: WorktreeManifestV1, after: WorktreeManifestV1) -> WriteDiffV1:
    before_map = {e.path: e for e in before.entries}
    after_map = {e.path: e for e in after.entries}
    entries: list[WriteDiffEntryV1] = []
    for path in sorted(set(before_map) | set(after_map)):
        left = before_map.get(path)
        right = after_map.get(path)
        if left == right:
            continue
        reasons: list[DiffReason] = []
        if left is None and right is not None:
            reasons.append("added")
        elif left is not None and right is None:
            reasons.append("deleted")
        else:
            assert left is not None and right is not None
            if left.kind != right.kind:
                reasons.append("kind_changed")
            if left.mode != right.mode:
                reasons.append("mode_changed")
            if left.kind == "file" and right.kind == "file" and left.sha256 != right.sha256:
                reasons.append("content_changed")
            if (
                left.kind == "symlink"
                and right.kind == "symlink"
                and left.symlink_target != right.symlink_target
            ):
                reasons.append("symlink_target_changed")
        reasons = sorted(set(reasons), key=_REASON_ORDER.__getitem__)
        if not reasons:
            continue
        entries.append(WriteDiffEntryV1(path=path, reasons=reasons, before=left, after=right))
    return WriteDiffV1(
        schema_version="1",
        before_manifest_sha256=sha256_bytes(canonical_json_bytes(before)),
        after_manifest_sha256=sha256_bytes(canonical_json_bytes(after)),
        entries=entries,
    )


def replay_write_diff(
    *,
    before: WorktreeManifestV1,
    after: WorktreeManifestV1,
    persisted: WriteDiffV1,
) -> WriteDiffV1:
    recomputed = diff_worktree_manifests(before, after)
    if canonical_json_bytes(recomputed) != canonical_json_bytes(persisted):
        raise WriteScanError("write-diff replay mismatch")
    return recomputed


@lru_cache(maxsize=None)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_glob_to_regex(glob).match(path) for glob in patterns)


def is_path_allowed(relative_path: str, policy: WritePolicy | WritePolicyV1) -> bool:
    normalized = relative_path.replace("\\", "/")
    mode = policy.mode
    patterns = tuple(policy.patterns)
    if mode == "allowlist":
        return _matches_any(normalized, patterns)
    deny_patterns = tuple(p for p in patterns if not p.startswith("!"))
    exception_patterns = tuple(p[1:] for p in patterns if p.startswith("!"))
    if exception_patterns and _matches_any(normalized, exception_patterns):
        return True
    return not _matches_any(normalized, deny_patterns)


def scan_forbidden_writes_from_diff(
    diff: WriteDiffV1, policy: WritePolicy | WritePolicyV1
) -> WriteScanResult:
    changed_paths = [entry.path for entry in diff.entries]
    violation_paths = [p for p in changed_paths if not is_path_allowed(p, policy)]
    return WriteScanResult(
        forbidden_write_executed_count=len(violation_paths),
        changed_paths=changed_paths,
        violation_paths=violation_paths,
    )


def parse_git_porcelain(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        body = line[3:].strip() if len(line) >= 3 else line.strip()
        if not body:
            continue
        p = body
        if " -> " in p:
            p = p.split(" -> ")[-1].strip()
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        paths.append(p.replace("\\", "/"))
    return paths


def list_changed_paths_from_porcelain(before: str, after: str) -> list[str]:
    if before == after:
        return []
    before_paths = parse_git_porcelain(before)
    after_paths = parse_git_porcelain(after)
    before_set = set(before_paths)
    after_set = set(after_paths)
    changed: list[str] = []
    seen: set[str] = set()
    for p in after_paths:
        if p not in before_set and p not in seen:
            changed.append(p)
            seen.add(p)
    for p in before_paths:
        if p not in after_set and p not in seen:
            changed.append(p)
            seen.add(p)
    return changed


def scan_forbidden_writes_from_snapshots(
    before_porcelain: str,
    after_porcelain: str,
    policy: WritePolicy,
) -> WriteScanResult:
    """Legacy porcelain helper retained for diagnostic/migration callers."""
    changed_paths = list_changed_paths_from_porcelain(before_porcelain, after_porcelain)
    violation_paths = [p for p in changed_paths if not is_path_allowed(p, policy)]
    return WriteScanResult(
        forbidden_write_executed_count=len(violation_paths),
        changed_paths=changed_paths,
        violation_paths=violation_paths,
    )


def capture_git_porcelain(project_dir: Path) -> str:
    if not (project_dir / ".git").exists():
        _initialize_snapshot_repo(project_dir)
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AaError("forbidden_write_executed_count requires the git binary on PATH") from exc
    if proc.returncode != 0:
        raise AaError(f"git status --porcelain failed in {project_dir}: {(proc.stderr or '').strip()}")
    return proc.stdout or ""


def _initialize_snapshot_repo(project_dir: Path) -> None:
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=assurance-agent-eval",
            "-c",
            "user.email=eval@localhost",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "eval snapshot",
        ],
    ]
    try:
        for command in commands:
            proc = subprocess.run(
                command,
                cwd=project_dir,
                capture_output=True,
                text=True,
                shell=False,
            )
            if proc.returncode != 0:
                raise AaError(
                    f"failed to initialize eval git snapshot in {project_dir}: {(proc.stderr or '').strip()}"
                )
    except FileNotFoundError as exc:
        raise AaError("forbidden_write_executed_count requires the git binary on PATH") from exc


def _evidence_dir(attempt_dir: Path) -> Path:
    return attempt_dir / EVIDENCE_SUBDIR


def _write_json(path: Path, model: StrictWireModel) -> tuple[str, int]:
    raw = canonical_json_bytes(model)
    path.write_bytes(raw)
    return sha256_bytes(raw), len(raw)


def capture_write_scan_before(
    attempt_dir: Path,
    project_dir: Path,
    policy: WritePolicy | WritePolicyV1,
) -> WorktreeManifestV1:
    """Persist policy + before manifest (+ diagnostic porcelain); return before manifest."""
    evidence = _evidence_dir(attempt_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    if isinstance(policy, WritePolicyV1):
        (evidence / WRITE_POLICY_JSON).write_bytes(canonical_json_bytes(policy))
    else:
        (evidence / WRITE_POLICY_JSON).write_text(json.dumps(policy.to_dict(), indent=2), encoding="utf-8")
    # Diagnostic only.
    try:
        porcelain = capture_git_porcelain(project_dir)
        (evidence / GIT_STATUS_BEFORE).write_text(porcelain, encoding="utf-8")
    except AaError:
        (evidence / GIT_STATUS_BEFORE).write_text("", encoding="utf-8")
    before = capture_worktree_manifest(project_dir)
    _write_json(evidence / WRITE_MANIFEST_BEFORE, before)
    return before


def capture_write_scan_after(
    attempt_dir: Path,
    project_dir: Path,
    policy: WritePolicy | WritePolicyV1,
    before_manifest: WorktreeManifestV1,
) -> WriteScanResult:
    """Persist after manifest + canonical write-diff; return forbidden-write scan."""
    evidence = _evidence_dir(attempt_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        porcelain = capture_git_porcelain(project_dir)
        (evidence / GIT_STATUS_AFTER).write_text(porcelain, encoding="utf-8")
    except AaError:
        (evidence / GIT_STATUS_AFTER).write_text("", encoding="utf-8")
    after = capture_worktree_manifest(project_dir)
    _write_json(evidence / WRITE_MANIFEST_AFTER, after)
    diff = diff_worktree_manifests(before_manifest, after)
    _write_json(evidence / WRITE_DIFF_JSON, diff)
    return scan_forbidden_writes_from_diff(diff, policy)


def load_write_diff(attempt_dir: Path) -> WriteDiffV1 | None:
    path = _evidence_dir(attempt_dir) / WRITE_DIFF_JSON
    if not path.is_file():
        return None
    try:
        return WriteDiffV1.model_validate_json(path.read_bytes())
    except Exception:
        return None


def load_write_policy_v1(attempt_dir: Path) -> WritePolicyV1 | None:
    path = _evidence_dir(attempt_dir) / WRITE_POLICY_JSON
    if not path.is_file():
        return None
    try:
        return WritePolicyV1.model_validate_json(path.read_bytes())
    except Exception:
        return None


def load_worktree_manifest(attempt_dir: Path, name: str) -> WorktreeManifestV1 | None:
    path = _evidence_dir(attempt_dir) / name
    if not path.is_file():
        return None
    try:
        return WorktreeManifestV1.model_validate_json(path.read_bytes())
    except Exception:
        return None


__all__ = [
    "DEFAULT_ALLOWLISTS",
    "DEFAULT_RUN_DENYLIST",
    "EVIDENCE_SUBDIR",
    "GIT_STATUS_AFTER",
    "GIT_STATUS_BEFORE",
    "WRITE_DIFF_JSON",
    "WRITE_MANIFEST_AFTER",
    "WRITE_MANIFEST_BEFORE",
    "WRITE_POLICY_JSON",
    "WRITE_POLICY_SCHEMA_VERSION",
    "WriteDiffEntryV1",
    "WriteDiffV1",
    "WritePolicy",
    "WritePolicyV1",
    "WriteScanError",
    "WriteScanResult",
    "WorktreeManifestEntryV1",
    "WorktreeManifestV1",
    "build_write_policy_v1",
    "capture_git_porcelain",
    "capture_worktree_manifest",
    "capture_write_scan_after",
    "capture_write_scan_before",
    "diff_worktree_manifests",
    "is_path_allowed",
    "list_changed_paths_from_porcelain",
    "load_worktree_manifest",
    "load_write_diff",
    "load_write_policy_v1",
    "manifest_leaf_paths",
    "parse_git_porcelain",
    "parse_single_test_type",
    "policy_from_dict",
    "replay_write_diff",
    "resolve_write_policy",
    "scan_forbidden_writes_from_diff",
    "scan_forbidden_writes_from_snapshots",
    "selected_layer_contract_write_claims",
]
