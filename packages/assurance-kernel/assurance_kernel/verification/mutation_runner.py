"""Injectable mutmut subprocess seam (§5-B1).

Workflow collectors depend on ``MutationTool`` so unit tests can mock discovery
and per-mutant evaluation without a real mutmut binary. The default
``SubprocessMutmutRunner`` wraps mutmut 3.7.x CLI / on-disk result formats.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from assurance_kernel.verification.mutation_sampling import MutantCandidate, SelectedMutant

MutantOutcome = Literal["killed", "survived", "equivalent", "error"]
MutationToolErrorKind = Literal["start_failed", "output_corrupt"]

_TOOL_ERROR_KINDS: frozenset[str] = frozenset({"start_failed", "output_corrupt"})
_RESULTS_LINE_RE = re.compile(r"^\s*(\S+):\s+\S+")
# mangled: module.path.x_func__mutmut_N  or  module.path.xǁClassǁmethod__mutmut_N
_MUTANT_NAME_RE = re.compile(
    r"^(?P<module>[\w.]+)\.(?P<mangled>x(?:_|\u01c1)[\w\u01c1]+)__mutmut_(?P<n>\d+)$"
)
_CLASS_SEP = "\u01c1"  # mutmut CLASS_NAME_SEPARATOR (ǁ)


class MutationToolError(Exception):
    """Typed mutmut adapter failure (start/output) — not a budget miss."""

    kind: MutationToolErrorKind
    detail: str

    def __init__(self, *, kind: MutationToolErrorKind, detail: str) -> None:
        if kind not in _TOOL_ERROR_KINDS:
            raise ValueError(f"unknown MutationToolError.kind: {kind!r}")
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class MutantResult:
    mutant: SelectedMutant
    outcome: MutantOutcome
    elapsed_seconds: float = 0.0
    detail: str = ""

    @property
    def locator(self) -> str:
        m = self.mutant
        return f"{m.module}:{m.line}:{m.operator}:{m.mutant_id}"


class MutationTool(Protocol):
    """Discover candidates and evaluate selected mutants under a timeout."""

    def discover(self, modules: Sequence[str]) -> Sequence[MutantCandidate]:
        """Return candidates for project-relative module paths."""
        ...

    def test(self, mutant: SelectedMutant, *, timeout_seconds: float) -> MutantResult:
        """Evaluate one selected mutant; may raise ``MutationToolError``."""
        ...


def candidate_from_mutmut_row(row: Mapping[str, object]) -> MutantCandidate:
    """Normalize one mutmut-style JSON/object row into ``MutantCandidate``."""
    mutant_id = row.get("mutant_id", row.get("id"))
    if mutant_id is None:
        raise MutationToolError(kind="output_corrupt", detail="mutant row missing id")
    try:
        return MutantCandidate(
            module=str(row["module"]),
            line=int(row["line"]),  # type: ignore[arg-type]
            operator=str(row["operator"]),
            mutant_id=str(mutant_id),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise MutationToolError(kind="output_corrupt", detail=f"bad mutant row: {err}") from err


def parse_mutmut_candidates(payload: str | bytes) -> tuple[MutantCandidate, ...]:
    """Parse a JSON list/object of mutant rows; corrupt input → typed error."""
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        raw = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise MutationToolError(kind="output_corrupt", detail=f"mutmut JSON corrupt: {err}") from err
    if isinstance(raw, dict) and "mutants" in raw:
        raw = raw["mutants"]
    if not isinstance(raw, list):
        raise MutationToolError(kind="output_corrupt", detail="mutmut candidates root must be a list")
    if any(not isinstance(item, Mapping) for item in raw):
        raise MutationToolError(kind="output_corrupt", detail="mutant row must be a mapping")
    return tuple(candidate_from_mutmut_row(item) for item in raw)


def parse_mutmut_results_text(text: str, *, project_root: Path | str) -> tuple[MutantCandidate, ...]:
    """Parse ``mutmut results [--all true]`` text lines into candidates."""
    root = Path(project_root)
    out: list[MutantCandidate] = []
    for line in text.splitlines():
        match = _RESULTS_LINE_RE.match(line)
        if match is None:
            continue
        out.append(candidate_from_mutmut_name(match.group(1), project_root=root))
    return tuple(out)


def candidate_from_mutmut_name(mutant_name: str, *, project_root: Path | str) -> MutantCandidate:
    """Build a candidate from a mutmut 3.7 mutant key (e.g. ``app.svc.x_add__mutmut_1``)."""
    root = Path(project_root)
    module = module_path_from_mutant_name(mutant_name)
    line = _resolve_function_line(root, module, mutant_name)
    return MutantCandidate(
        module=module,
        line=line,
        operator="mutmut",
        mutant_id=mutant_name,
    )


def module_path_from_mutant_name(mutant_name: str) -> str:
    """``app.svc.x_add__mutmut_1`` → ``app/svc.py``."""
    match = _MUTANT_NAME_RE.match(mutant_name)
    if match is not None:
        return match.group("module").replace(".", "/") + ".py"
    # Fallback: strip trailing ``.x_*__mutmut_N`` / ``.xǁ…__mutmut_N``.
    head, _, tail = mutant_name.rpartition(".")
    if head and ("__mutmut_" in tail):
        return head.replace(".", "/") + ".py"
    return mutant_name.replace(".", "/") + ".py"


def discover_from_meta_dir(project_root: Path | str) -> tuple[MutantCandidate, ...]:
    """Read mutmut 3.7 on-disk ``mutants/**/*.meta`` JSON (``exit_code_by_key``)."""
    root = Path(project_root)
    meta_root = root / "mutants"
    if not meta_root.is_dir():
        return ()
    names: list[str] = []
    for path in sorted(meta_root.rglob("*.meta")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        keys = raw.get("exit_code_by_key")
        if not isinstance(keys, Mapping):
            continue
        names.extend(str(key) for key in keys)
    return tuple(candidate_from_mutmut_name(name, project_root=root) for name in names)


@dataclass(frozen=True)
class SubprocessMutmutRunner:
    """Default mutmut 3.7.x CLI adapter.

    Discover mapping (in order):
    1. on-disk ``mutants/**/*.meta`` JSON ``exit_code_by_key``
    2. ``mutmut results --all true`` text lines (``name: status``)

    Test mapping: ``mutmut run <mutant_id>``; outcome from emoji/status lines
    (CLI exit 0 is not treated as killed — mutmut exits 0 for survived too).
    """

    project_root: Path | str
    mutmut_bin: str = "mutmut"

    def discover(self, modules: Sequence[str]) -> Sequence[MutantCandidate]:
        root = Path(self.project_root)
        wanted = {str(m) for m in modules}
        from_disk = discover_from_meta_dir(root)
        if from_disk:
            return _filter_modules(from_disk, wanted)

        try:
            proc = subprocess.run(
                [self.mutmut_bin, "results", "--all", "true"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except FileNotFoundError as err:
            raise MutationToolError(kind="start_failed", detail=f"{self.mutmut_bin} not found") from err
        except OSError as err:
            raise MutationToolError(kind="start_failed", detail=str(err)) from err

        combined = f"{proc.stdout}\n{proc.stderr}"
        if (
            "--json" in (proc.args if isinstance(proc.args, list) else [])
            or "No such option: --json" in combined
        ):
            raise MutationToolError(
                kind="start_failed",
                detail="mutmut results does not support --json; use --all true or parse .meta",
            )
        if proc.returncode != 0 and not proc.stdout.strip():
            detail = (proc.stderr or proc.stdout or "mutmut results failed").strip()
            raise MutationToolError(kind="start_failed", detail=detail)
        return _filter_modules(parse_mutmut_results_text(proc.stdout, project_root=root), wanted)

    def test(self, mutant: SelectedMutant, *, timeout_seconds: float) -> MutantResult:
        root = Path(self.project_root)
        try:
            proc = subprocess.run(
                [self.mutmut_bin, "run", mutant.mutant_id],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=max(timeout_seconds, 0.1),
            )
        except FileNotFoundError as err:
            raise MutationToolError(kind="start_failed", detail=f"{self.mutmut_bin} not found") from err
        except subprocess.TimeoutExpired:
            return MutantResult(
                mutant=mutant, outcome="survived", elapsed_seconds=timeout_seconds, detail="timeout"
            )
        except OSError as err:
            raise MutationToolError(kind="start_failed", detail=str(err)) from err

        outcome = _outcome_from_mutmut_output(proc.stdout, proc.stderr, proc.returncode)
        return MutantResult(mutant=mutant, outcome=outcome, elapsed_seconds=0.0, detail=proc.stderr.strip())


def _filter_modules(
    candidates: Sequence[MutantCandidate],
    wanted: set[str],
) -> tuple[MutantCandidate, ...]:
    if not wanted:
        return tuple(candidates)
    return tuple(c for c in candidates if c.module in wanted)


def _resolve_function_line(project_root: Path, module: str, mutant_name: str) -> int:
    """Best-effort source line: containing function/method def line (1-based)."""
    func_name, class_name = _function_and_class_from_mutant_name(mutant_name)
    path = project_root / module
    if not path.is_file():
        return 1
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return 1
    for node in tree.body:
        if class_name and isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_name:
                    return int(item.lineno)
        if (
            not class_name
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            return int(node.lineno)
    return 1


def _function_and_class_from_mutant_name(mutant_name: str) -> tuple[str, str | None]:
    match = _MUTANT_NAME_RE.match(mutant_name)
    mangled = match.group("mangled") if match is not None else mutant_name.rpartition(".")[-1]
    mangled = re.sub(r"__mutmut_\d+$", "", mangled)
    if _CLASS_SEP in mangled:
        # xǁClassǁmethod
        parts = mangled.split(_CLASS_SEP)
        if len(parts) >= 3:
            return parts[-1], parts[1]
    if mangled.startswith("x_"):
        return mangled[2:], None
    return mangled, None


def _outcome_from_mutmut_output(stdout: str, stderr: str, returncode: int) -> MutantOutcome:
    """Map mutmut 3.7 run output to outcomes.

    mutmut prints status emoji next to the mutant name and nearly always exits 0
    for a successful CLI invocation — exit code must not be treated as killed.
    """
    del returncode  # CLI exit ≠ mutant kill/survive
    blob = f"{stdout}\n{stderr}"
    lower = blob.lower()
    if "equivalent" in lower:
        return "equivalent"
    # Prefer the dedicated "Mutant results" block when present.
    results_block = blob
    marker = "Mutant results"
    if marker in blob:
        results_block = blob[blob.index(marker) :]
    if "🎉" in results_block or re.search(r":\s*killed\b", results_block, re.I):
        return "killed"
    if "🙁" in results_block or "survived" in lower or "bad luck" in lower:
        return "survived"
    if "⏰" in results_block or "timeout" in lower:
        return "survived"
    if "🧙" in results_block or "caught by type check" in lower:
        return "killed"
    if "💥" in results_block or "segfault" in lower:
        return "error"
    if "🫥" in results_block or "no tests" in lower:
        return "error"
    if "killed" in lower:
        return "killed"
    return "error"


__all__ = [
    "MutantOutcome",
    "MutantResult",
    "MutationTool",
    "MutationToolError",
    "MutationToolErrorKind",
    "SubprocessMutmutRunner",
    "candidate_from_mutmut_name",
    "candidate_from_mutmut_row",
    "discover_from_meta_dir",
    "module_path_from_mutant_name",
    "parse_mutmut_candidates",
    "parse_mutmut_results_text",
]
