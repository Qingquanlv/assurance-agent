from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from graph_engine.boot.boot import BootValidationError
from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS, RuntimeKind

_TEST_SELECTOR: Callable[[str], RuntimeKind] | None = None
_TEST_CRASH_AT: str | None = None
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class SelectionCrash(RuntimeError):
    """Raised by the test-only handshake crash injector."""


class RuntimeSelectionError(ValueError):
    """Raised when a selection record or runtime evidence is invalid."""


class LegacyRuntimeRecord(FrozenModel):
    schema_version: Literal["1"] = "1"
    runtime: Literal["legacy-v2"] = "legacy-v2"
    phase: Literal["initializing", "initialized"]
    invocation_id: str
    entrypoint: str
    root_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_digest: str | None = None


class LangGraphRuntimeRecord(FrozenModel):
    schema_version: Literal["1"] = "1"
    runtime: Literal["langgraph-v1"] = "langgraph-v1"
    phase: Literal["initializing", "initialized"]
    invocation_id: str
    entrypoint: str
    root_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_digest: str | None = None


SelectionRecord = LegacyRuntimeRecord | LangGraphRuntimeRecord


def use_test_runtime_selector(selector: Callable[[str], RuntimeKind] | None) -> None:
    global _TEST_SELECTOR
    _TEST_SELECTOR = selector
    if selector is None:
        global _TEST_CRASH_AT
        _TEST_CRASH_AT = None


ENTRYPOINT_AGENT_CONTRACT_IDS: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        "archive": ("assurance.improvement.agent.archive.v1",),
        "case": (
            "assurance.intake.agent.case-design.v1",
            "assurance.intake.agent.case-review.v1",
        ),
        "execute": (
            "assurance.execution.agent.execute.v1",
            "assurance.execution.agent.run.v1",
            "assurance.generation.agent.api.plan.v1",
            "assurance.healing.agent.coverage-repair.v1",
            "assurance.healing.agent.fix-proposal.v1",
            "assurance.quality.agent.fact-baseline.v1",
            "assurance.quality.agent.inspect.v1",
            "assurance.quality.agent.report.v1",
        ),
        "full": (
            "assurance.execution.agent.execute.v1",
            "assurance.generation.agent.api.plan.v1",
            "assurance.improvement.agent.archive.v1",
            "assurance.intake.agent.intake.v1",
            "assurance.quality.agent.report.v1",
        ),
        "improvement-apply": (),
        "improvement-evaluate": (),
        "improvement-export": (),
        "improvement-review": ("assurance.improvement.agent.improvement-review.v1",),
        "improvement-rollback": (),
        "intake": (
            "assurance.intake.agent.intake.v1",
            "assurance.intake.agent.explore.v1",
            "assurance.intake.agent.case-design.v1",
            "assurance.intake.agent.case-review.v1",
        ),
        "issue-analyze": ("assurance.quality.agent.issue-analysis.v1",),
        "issue-reconcile": ("assurance.quality.agent.issue-analysis.v1",),
        "issue-review": ("assurance.quality.agent.issue-triage.v1",),
        "retro": (
            "assurance.improvement.agent.retro.v1",
            "assurance.improvement.agent.retro-eval-analysis.v1",
            "assurance.improvement.agent.retro-issue-analysis.v1",
            "assurance.improvement.agent.retro-workflow-analysis.v1",
        ),
    }
)


def validate_entrypoint_runtime_cutover(mapping: Mapping[str, str]) -> None:
    expected = set(PRODUCT_ENTRYPOINTS)
    got = set(mapping)
    missing = expected - got
    extra = got - expected
    if missing or extra:
        raise BootValidationError(
            "runtime cutover keys must be the exact 14 public names; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    invalid = {name: kind for name, kind in mapping.items() if kind not in {"legacy-v2", "langgraph-v1"}}
    if invalid:
        raise BootValidationError(f"runtime cutover values must be legacy-v2 or langgraph-v1: {invalid}")


def entrypoint_requires_provider_schema(entrypoint: str) -> bool:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    contracts = all_feature_agent_contracts()
    for contract_id in ENTRYPOINT_AGENT_CONTRACT_IDS[entrypoint]:
        contract = contracts.get(contract_id)
        if contract is not None and contract.requires_provider_schema:
            return True
    return False


def select_runtime(entrypoint: str) -> RuntimeKind:
    if _TEST_SELECTOR is not None:
        return _TEST_SELECTOR(entrypoint)
    validate_entrypoint_runtime_cutover(ENTRYPOINT_RUNTIME_CUTOVER)
    try:
        return ENTRYPOINT_RUNTIME_CUTOVER[entrypoint]
    except KeyError as error:
        raise RuntimeSelectionError(f"unknown product entrypoint: {entrypoint}") from error


def selection_path(workspace: ChangeWorkspace, invocation_id: str) -> Path:
    return workspace.paths.langgraph_selections / f"{invocation_id}.json"


def maybe_crash(point: str) -> None:
    if _TEST_CRASH_AT == point:
        raise SelectionCrash(point)


def load_selection(workspace: ChangeWorkspace, invocation_id: str) -> SelectionRecord | None:
    path = selection_path(workspace, invocation_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeSelectionError("selection record must be a regular file")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeSelectionError("selection record is corrupt") from error
    except Exception as error:
        raise RuntimeSelectionError("selection record is corrupt") from error
    if not isinstance(payload, dict):
        raise RuntimeSelectionError("selection record is corrupt")
    runtime = payload.get("runtime")
    try:
        if runtime == "legacy-v2":
            return LegacyRuntimeRecord.model_validate(payload)
        if runtime == "langgraph-v1":
            return LangGraphRuntimeRecord.model_validate(payload)
    except Exception as error:
        raise RuntimeSelectionError("selection record is corrupt") from error
    raise RuntimeSelectionError("selection record runtime is unknown")


def write_initializing(workspace: ChangeWorkspace, record: SelectionRecord) -> SelectionRecord:
    if record.phase != "initializing":
        raise RuntimeSelectionError("phase 1 must write an initializing record")
    workspace.paths.langgraph_root.mkdir(mode=0o700, exist_ok=True)
    workspace.paths.langgraph_selections.mkdir(mode=0o700, exist_ok=True)
    path = selection_path(workspace, record.invocation_id)
    encoded = _canonical_bytes(record)
    with _namespace_lock(workspace):
        existing = load_selection(workspace, record.invocation_id)
        if existing is not None:
            _assert_same_identity(existing, record)
            if existing.phase in {"initializing", "initialized"}:
                return existing
            raise RuntimeSelectionError("selection record disagrees with the requested identity")
        _atomic_replace(path, encoded)
    maybe_crash("after_initializing")
    return record


def complete_initialized(workspace: ChangeWorkspace, record: SelectionRecord) -> SelectionRecord:
    if record.phase != "initialized" or not record.identity_digest:
        raise RuntimeSelectionError("phase 3 must write an initialized identity digest")
    path = selection_path(workspace, record.invocation_id)
    encoded = _canonical_bytes(record)
    with _namespace_lock(workspace):
        existing = load_selection(workspace, record.invocation_id)
        if existing is None:
            raise RuntimeSelectionError("initialized replacement requires an initializing record")
        _assert_same_identity(existing, record)
        if existing.phase == "initialized":
            if _canonical_bytes(existing) != encoded:
                raise RuntimeSelectionError("initialized selection record disagrees with evidence")
            return existing
        maybe_crash("before_initialized")
        _atomic_replace(path, encoded)
    return record


def backfill_legacy(
    workspace: ChangeWorkspace,
    *,
    invocation_id: str,
    entrypoint: str,
    root_input_digest: str,
    build_identity: str,
    identity_digest: str,
) -> LegacyRuntimeRecord:
    existing = load_selection(workspace, invocation_id)
    record = LegacyRuntimeRecord(
        phase="initialized",
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        root_input_digest=root_input_digest,
        build_identity=build_identity,
        identity_digest=identity_digest,
    )
    if existing is not None:
        if _canonical_bytes(existing) != _canonical_bytes(record):
            raise RuntimeSelectionError("backfill disagrees with an existing selection record")
        if not isinstance(existing, LegacyRuntimeRecord):
            raise RuntimeSelectionError("backfill disagrees with an existing selection record")
        return existing
    workspace.paths.langgraph_root.mkdir(mode=0o700, exist_ok=True)
    workspace.paths.langgraph_selections.mkdir(mode=0o700, exist_ok=True)
    with _namespace_lock(workspace):
        again = load_selection(workspace, invocation_id)
        if again is not None:
            if _canonical_bytes(again) != _canonical_bytes(record):
                raise RuntimeSelectionError("backfill disagrees with an existing selection record")
            if not isinstance(again, LegacyRuntimeRecord):
                raise RuntimeSelectionError("backfill disagrees with an existing selection record")
            return again
        _atomic_replace(selection_path(workspace, invocation_id), _canonical_bytes(record))
    return record


def require_initialized(record: SelectionRecord) -> SelectionRecord:
    if record.phase != "initialized" or not record.identity_digest:
        raise RuntimeSelectionError("only an initialized selection record is resumable")
    return record


def _assert_same_identity(existing: SelectionRecord, requested: SelectionRecord) -> None:
    if (
        existing.runtime != requested.runtime
        or existing.invocation_id != requested.invocation_id
        or existing.entrypoint != requested.entrypoint
        or existing.root_input_digest != requested.root_input_digest
        or existing.build_identity != requested.build_identity
    ):
        raise RuntimeSelectionError("selection record disagrees with the requested identity")


def _canonical_bytes(record: SelectionRecord) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _atomic_replace(path: Path, encoded: bytes) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeSelectionError("selection record must be a regular file")
    pending = path.with_name(f".{path.name}.pending")
    pending.write_bytes(encoded)
    os.replace(pending, path)


def _namespace_lock(workspace: ChangeWorkspace):
    workspace.paths.langgraph_selections.mkdir(mode=0o700, exist_ok=True)

    class _Lock:
        def __enter__(self) -> None:
            self._fd = os.open(str(workspace.paths.langgraph_selections), _DIRECTORY_FLAGS)
            fcntl.flock(self._fd, fcntl.LOCK_EX)

        def __exit__(self, *_args: object) -> None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)

    return _Lock()


def record_digest(record: SelectionRecord) -> str:
    return canonical_digest(record.model_dump(mode="json"))


if set(ENTRYPOINT_AGENT_CONTRACT_IDS) != set(PRODUCT_ENTRYPOINTS):
    raise RuntimeError("entrypoint Agent-contract inventory must cover the 14 public names")

__all__ = [
    "ENTRYPOINT_AGENT_CONTRACT_IDS",
    "LangGraphRuntimeRecord",
    "LegacyRuntimeRecord",
    "RuntimeSelectionError",
    "SelectionCrash",
    "SelectionRecord",
    "backfill_legacy",
    "complete_initialized",
    "entrypoint_requires_provider_schema",
    "load_selection",
    "maybe_crash",
    "require_initialized",
    "select_runtime",
    "selection_path",
    "use_test_runtime_selector",
    "validate_entrypoint_runtime_cutover",
    "write_initializing",
]
