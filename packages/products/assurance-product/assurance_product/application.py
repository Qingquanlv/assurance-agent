from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import Field

from graph_engine.application import AssuranceApplication, AssuranceRuntimeContext, StartedInvocation
from graph_engine.boot.graph_revision import GraphBuildManifest
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.composition.lock import InvocationLock, ProductLock
from graph_engine.evidence.events import InvocationStarted
from graph_engine.evidence.legacy_v2 import (
    authenticate_invocation_lock_v2,
    fold_legacy_events,
    read_legacy_ledger,
)
from graph_engine.evidence.models import InvocationProjection
from graph_engine.attempts.secret_sources import InvocationRuntimeAuthorization
from graph_engine.plugin_api import FrozenModel

from assurance_product.binding_builder import build_deployment_wheel
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.export import publish_achieved, select_publish_change
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, StatusV1
from assurance_product.product import (
    coexistence_graph_manifest,
    product_lock_from_composition,
    reject_organization_overrides,
)
from assurance_product.revision_registry import (
    RevisionRegistry,
    RevisionRegistryError,
    assert_recorded_revision,
)
from assurance_product.runtime_ports import ProductRuntimePorts
from assurance_product.status import (
    archive_published,
    render_status,
    render_status_from_langgraph,
    write_runtime_projections,
)

_TEST_CRASH_AT: str | None = None
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class SelectionCrash(RuntimeError):
    """Raised by the test-only handshake crash injector."""


class RuntimeSelectionError(ValueError):
    """Raised when an invocation identity record or runtime evidence is invalid."""


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
    encoded = _identity_bytes(record)
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
    encoded = _identity_bytes(record)
    with _namespace_lock(workspace):
        existing = load_selection(workspace, record.invocation_id)
        if existing is None:
            raise RuntimeSelectionError("initialized replacement requires an initializing record")
        _assert_same_identity(existing, record)
        if existing.phase == "initialized":
            if _identity_bytes(existing) != encoded:
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
        if _identity_bytes(existing) != _identity_bytes(record):
            raise RuntimeSelectionError("backfill disagrees with an existing selection record")
        if not isinstance(existing, LegacyRuntimeRecord):
            raise RuntimeSelectionError("backfill disagrees with an existing selection record")
        return existing
    workspace.paths.langgraph_root.mkdir(mode=0o700, exist_ok=True)
    workspace.paths.langgraph_selections.mkdir(mode=0o700, exist_ok=True)
    with _namespace_lock(workspace):
        again = load_selection(workspace, invocation_id)
        if again is not None:
            if _identity_bytes(again) != _identity_bytes(record):
                raise RuntimeSelectionError("backfill disagrees with an existing selection record")
            if not isinstance(again, LegacyRuntimeRecord):
                raise RuntimeSelectionError("backfill disagrees with an existing selection record")
            return again
        _atomic_replace(selection_path(workspace, invocation_id), _identity_bytes(record))
    return record


def require_initialized(record: SelectionRecord) -> SelectionRecord:
    if record.phase != "initialized" or not record.identity_digest:
        raise RuntimeSelectionError("only an initialized selection record is resumable")
    return record


def record_digest(record: SelectionRecord) -> str:
    return canonical_digest(record.model_dump(mode="json"))


def _assert_same_identity(existing: SelectionRecord, requested: SelectionRecord) -> None:
    if (
        existing.runtime != requested.runtime
        or existing.invocation_id != requested.invocation_id
        or existing.entrypoint != requested.entrypoint
        or existing.root_input_digest != requested.root_input_digest
        or existing.build_identity != requested.build_identity
    ):
        raise RuntimeSelectionError("selection record disagrees with the requested identity")


def _identity_bytes(record: SelectionRecord) -> bytes:
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


if set(ENTRYPOINT_AGENT_CONTRACT_IDS) != set(PRODUCT_ENTRYPOINTS):
    raise RuntimeError("entrypoint Agent-contract inventory must cover the 14 public names")


_LG_EXIT = {
    "completed": (0, "completed"),
    "running": (20, "running"),
    "blocked": (20, "blocked"),
    "stopped": (20, "stopped"),
    "interrupted": (30, "interrupted"),
    "failed": (40, "failed"),
}


class ProductBuildArtifacts:
    def __init__(
        self,
        product_lock: ProductLock | Mapping[str, object],
        graph_manifest: GraphBuildManifest | Mapping[str, object],
    ) -> None:
        self.product_lock = product_lock if isinstance(product_lock, ProductLock) else _LockView(product_lock)
        self.graph_manifest = (
            graph_manifest
            if isinstance(graph_manifest, GraphBuildManifest)
            else _ManifestView(graph_manifest)
        )

    def model_dump(self) -> dict[str, object]:
        return {
            "product_lock": self.product_lock.model_dump(mode="json"),
            "graph_manifest": self.graph_manifest.model_dump(mode="json"),
        }


class _LockView:
    def __init__(self, lock: InvocationLock | ProductLock | Mapping[str, object]) -> None:
        if isinstance(lock, Mapping):
            self._data = dict(lock)
            self.schema_version = str(lock["schema_version"])
            self.digest = str(lock["digest"])
            return
        self._data = lock.model_dump(mode="json")
        self.schema_version = str(self._data["schema_version"])
        self.digest = lock.digest

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return dict(self._data)


class _ManifestView:
    def __init__(self, manifest: GraphBuildManifest | Mapping[str, object]) -> None:
        if isinstance(manifest, Mapping):
            self._data = dict(manifest)
            revision = manifest["revision"]
            if not isinstance(revision, Mapping):
                raise TypeError("graph manifest revision must be a mapping")
            self.revision = _RevisionView(revision)
            return
        self._data = manifest.model_dump(mode="json")
        self.revision = manifest.revision

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return dict(self._data)


class _RevisionView:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.revision_id = str(payload["revision_id"])
        self._data = dict(payload)

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return dict(self._data)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate interrupt ids")
    return dict(pairs)


def parse_resume_file(path: Path, pending_ids: Sequence[str] = ()) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("resume file is not valid JSON") from error
    pending = tuple(pending_ids)
    if isinstance(payload, str):
        if len(pending) > 1:
            raise ValueError("ambiguous scalar resume")
        return payload
    if not isinstance(payload, dict):
        raise ValueError("resume file must be an object or scalar action")
    if "interrupts" in payload:
        mapping = payload["interrupts"]
        if not isinstance(mapping, dict):
            raise ValueError("interrupt mapping is invalid")
        if len(mapping) != len(set(mapping)):
            raise ValueError("duplicate interrupt ids")
        if pending and set(mapping) != set(pending):
            raise ValueError("unknown or missing interrupt ids")
        return mapping
    interrupt_id = payload.get("interrupt_id")
    if isinstance(interrupt_id, str):
        if pending and interrupt_id not in pending:
            raise ValueError("unknown interrupt id")
        return {interrupt_id: payload}
    if "wakeup" in payload or "reconciliation" in payload or "action" in payload:
        return payload
    raise ValueError("resume file is missing a validated envelope")


class AssuranceProductApplication:
    def __init__(self) -> None:
        return None

    def compile(
        self,
        composition: Any,
        *,
        product: str,
        config_tree: str,
    ) -> ProductBuildArtifacts:
        reject_organization_overrides(Path(config_tree))
        product_lock = product_lock_from_composition(composition)
        if product_lock.schema_version != "3":
            raise ValueError("aa compile emits only ProductLock v3")
        manifest = coexistence_graph_manifest(composition, product_lock)
        return ProductBuildArtifacts(product_lock, manifest)

    def start(
        self,
        *,
        project_dir: Path,
        change_id: str,
        invocation_id: str,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        entrypoint: str,
        input_path: Path,
        workspace: ChangeWorkspace,
    ) -> dict[str, object]:
        if entrypoint not in PRODUCT_ENTRYPOINTS:
            raise ValueError(f"unknown product entrypoint: {entrypoint}")
        product_input = _load_input(input_path, entrypoint=entrypoint, composition=composition)
        if product_input.change_id != change_id:
            raise ValueError("change does not match product input change_id")
        root_input_data = product_input.model_dump(mode="json")
        root_input = cast(JSONValue, root_input_data)
        root_input_digest = canonical_digest(cast(JSONValue, root_input_data))
        product_lock = product_lock_from_composition(composition)
        existing = load_selection(workspace, invocation_id)
        if existing is not None:
            runtime = existing.runtime
            build_identity = existing.build_identity
            if existing.entrypoint != entrypoint or existing.root_input_digest != root_input_digest:
                raise RuntimeSelectionError("selection record disagrees with the requested identity")
            if existing.phase == "initialized":
                return {
                    "invocation_id": invocation_id,
                    "change_id": change_id,
                    "lock_digest": composition.lock_digest,
                    "composition_digest": composition.digest,
                    "root_input_digest": root_input_digest,
                }
        else:
            runtime = "langgraph-v1"
            build_identity = product_lock.digest
        if runtime == "legacy-v2":
            raise RuntimeSelectionError("leftover workflow execution is deleted")
        initializing = LangGraphRuntimeRecord(
            phase="initializing",
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=root_input_digest,
            build_identity=build_identity,
        )
        write_initializing(workspace, initializing)
        _bind_revision(
            workspace,
            invocation_id=invocation_id,
            runtime=runtime,
            composition=composition,
            product_lock=product_lock,
            build_identity=build_identity,
        )
        identity_digest = asyncio.run(
            self._start_langgraph(
                workspace=workspace,
                composition=composition,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input=cast(Mapping[str, JSONValue], root_input),
                product_lock=product_lock,
            )
        )
        maybe_crash("after_identity")
        complete_initialized(
            workspace,
            LangGraphRuntimeRecord(
                phase="initialized",
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input_digest=root_input_digest,
                build_identity=build_identity,
                identity_digest=identity_digest,
            ),
        )
        return {
            "invocation_id": invocation_id,
            "change_id": change_id,
            "lock_digest": composition.lock_digest,
            "composition_digest": composition.digest,
            "root_input_digest": root_input_digest,
        }

    def run(
        self,
        *,
        project_dir: Path,
        change_id: str,
        invocation_id: str,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        entrypoint: str | None,
        input_path: Path | None,
        workspace: ChangeWorkspace,
        secrets: Sequence[str],
    ) -> tuple[SimpleRun, str, int]:
        del project_dir, secrets
        existing = load_selection(workspace, invocation_id)
        needs_handshake = existing is None and not _legacy_invocation_exists(workspace, invocation_id)
        needs_finish = existing is not None and existing.phase != "initialized"
        if needs_handshake or needs_finish:
            if entrypoint is None or input_path is None:
                if needs_finish:
                    raise RuntimeSelectionError("only an initialized selection record is resumable")
                raise ValueError("first run requires --entrypoint and --input")
            document = self.start(
                project_dir=workspace.paths.project_root,
                change_id=change_id,
                invocation_id=invocation_id,
                composition=composition,
                authorization=authorization,
                entrypoint=entrypoint,
                input_path=input_path,
                workspace=workspace,
            )
            del document
        record = require_initialized(
            self._resolve_existing(
                workspace,
                invocation_id,
                composition=composition,
                authorization=authorization,
            )
        )
        if record.runtime == "legacy-v2":
            raise RuntimeSelectionError("leftover workflow execution is deleted")
        status = asyncio.run(
            self._run_langgraph(
                workspace=workspace,
                composition=composition,
                invocation_id=invocation_id,
                record=record,
            )
        )
        code, mapped = _LG_EXIT[status]
        return SimpleRun(status=status, terminal_reason=None, actions=(), projection=None), mapped, code

    def resume(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        action: str | None,
        reason: str | None,
        resume_file: Path | None,
    ) -> tuple[SimpleRun, str, int]:
        record = self._resolve_existing(
            workspace,
            invocation_id,
            composition=composition,
            authorization=authorization,
        )
        require_initialized(record)
        if record.runtime == "legacy-v2":
            raise RuntimeSelectionError("leftover workflow execution is deleted")
        resume_payload: object
        _assert_langgraph_revision(workspace, composition, invocation_id)
        if resume_file is not None:
            pending_ids = asyncio.run(
                self._pending_interrupt_ids(
                    workspace=workspace,
                    composition=composition,
                    invocation_id=invocation_id,
                    record=record,
                )
            )
            resume_payload = parse_resume_file(resume_file, pending_ids=pending_ids)
        else:
            resume_payload = {"action": action, "reason": reason}
        if isinstance(resume_payload, dict) and "wakeup" in resume_payload:
            from graph_engine.attempts.resolutions import PendingTaskResult

            resume_payload = PendingTaskResult.model_validate(resume_payload)
        elif isinstance(resume_payload, dict) and "reconciliation" in resume_payload:
            from graph_engine.attempts.resolutions import IndeterminateTaskResult

            resume_payload = IndeterminateTaskResult.model_validate(resume_payload)
        status = asyncio.run(
            self._resume_langgraph(
                workspace=workspace,
                composition=composition,
                invocation_id=invocation_id,
                record=record,
                resume=resume_payload,
            )
        )
        code, mapped = _LG_EXIT[status]
        return SimpleRun(status=status, terminal_reason=None, actions=(), projection=None), mapped, code

    def status(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        change_id: str,
    ) -> StatusV1:
        record = self._resolve_existing(
            workspace,
            invocation_id,
            composition=composition,
            authorization=authorization,
        )
        if record.runtime == "legacy-v2":
            projection, identity = self._historical_legacy_projection(workspace, invocation_id)
            return render_status(
                projection,
                root_input_digest=identity["root_input_digest"],
                change_id=change_id,
            )
        status_name, snapshot, journal_events = asyncio.run(
            self._status_langgraph(workspace, composition, invocation_id, record)
        )
        return render_status_from_langgraph(
            invocation_id=invocation_id,
            lock_digest=record.build_identity,
            root_input_digest=record.root_input_digest,
            entrypoint=record.entrypoint,
            change_id=change_id,
            status=status_name,
            snapshot=snapshot,
            journal_events=journal_events,
        )

    def lock_show(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
    ) -> dict[str, object]:
        record = self._resolve_existing(
            workspace,
            invocation_id,
            composition=composition,
            authorization=authorization,
        )
        if record.runtime == "legacy-v2":
            if _langgraph_db_has_invocation(workspace, invocation_id):
                raise RuntimeSelectionError("runtime evidence is ambiguous")
            lock_path = workspace.paths.runtime_root / "invocations" / invocation_id / "invocation.lock.json"
            lock = authenticate_invocation_lock_v2(lock_path.read_bytes())
            return {
                "lock_digest": lock.digest,
                "engine_api": lock.engine_api,
                "lock": json.loads(lock.canonical_bytes.decode("utf-8")),
            }
        if _legacy_invocation_exists(workspace, invocation_id):
            raise RuntimeSelectionError("runtime evidence is ambiguous")
        product_lock = product_lock_from_composition(composition)
        manifest = coexistence_graph_manifest(composition, product_lock)
        return {
            "lock_digest": product_lock.digest,
            "engine_api": product_lock.engine_api,
            "lock": product_lock.model_dump(mode="json"),
            "revision": manifest.revision.model_dump(mode="json"),
        }

    def export(self, *, project_dir: Path, change_id: str | None) -> dict[str, object]:
        project = Path(project_dir).resolve()
        selected = select_publish_change(project, change_id)
        ChangeWorkspace.open(project, selected)
        return publish_achieved(project, selected).model_dump(mode="json")

    def archive(self, *, project_dir: Path, change_id: str) -> dict[str, object]:
        return archive_published(Path(project_dir).resolve(), change_id)

    def bindings_build(self, *, manifest: Path, output_dir: Path) -> dict[str, object]:
        built = build_deployment_wheel(manifest, output_dir)
        return {
            "wheel": str(built.wheel),
            "distribution": built.distribution,
            "declaration_path": built.declaration_path,
            "manifest_digest": built.manifest_digest,
            "wheel_digest": built.wheel_digest,
            "entry_point_value": built.entry_point_value,
        }

    def _resolve_existing(
        self,
        workspace: ChangeWorkspace,
        invocation_id: str,
        *,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
    ) -> SelectionRecord:
        record = load_selection(workspace, invocation_id)
        if record is not None:
            if record.runtime == "langgraph-v1" and _legacy_invocation_exists(workspace, invocation_id):
                raise RuntimeSelectionError("both-runtime artifacts are present")
            if record.runtime == "langgraph-v1" and record.phase == "initialized":
                row = _langgraph_started_row(workspace, invocation_id)
                identity = _langgraph_identity_digest(workspace, invocation_id)
                if row is None or identity is None:
                    raise RuntimeSelectionError("langgraph evidence is absent")
                if (
                    record.identity_digest != identity
                    or record.build_identity != row[2]
                    or record.root_input_digest != row[3]
                ):
                    raise RuntimeSelectionError("selection record disagrees with langgraph evidence")
            if record.runtime == "legacy-v2" and record.phase == "initialized":
                identity = _legacy_lock_digest(workspace, invocation_id)
                if identity is None:
                    raise RuntimeSelectionError("legacy evidence is absent")
                if (
                    record.identity_digest != identity
                    or record.build_identity != identity
                    or record.identity_digest != record.build_identity
                ):
                    raise RuntimeSelectionError("selection record disagrees with legacy evidence")
            return record
        if not _legacy_invocation_exists(workspace, invocation_id):
            raise RuntimeSelectionError("invocation evidence is absent")
        if _langgraph_db_has_invocation(workspace, invocation_id):
            raise RuntimeSelectionError("both-runtime artifacts are present")
        projection, identity = self._historical_legacy_projection(workspace, invocation_id)
        return backfill_legacy(
            workspace,
            invocation_id=invocation_id,
            entrypoint=projection.entrypoint or "",
            root_input_digest=identity["root_input_digest"],
            build_identity=composition.lock.digest,
            identity_digest=composition.lock.digest,
        )

    def _historical_legacy_projection(
        self,
        workspace: ChangeWorkspace,
        invocation_id: str,
    ) -> tuple[InvocationProjection, dict[str, str]]:
        return _read_historical_legacy(workspace, invocation_id)

    async def _start_langgraph(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        invocation_id: str,
        entrypoint: str,
        root_input: Mapping[str, JSONValue],
        product_lock: ProductLock,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            RevisionRegistry(workspace).remember(
                coexistence_graph_manifest(composition, product_lock).revision
            )
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=canonical_digest(dict(root_input)),
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
                start_pins=ports.backend,
            )
            started = await application.start(
                artifact=artifact,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=root_input,
                runtime_context=_context(ports, artifact),
            )
            del started
            maybe_crash("after_identity")
            recovered = await ports.backend.recover_handshake(invocation_id)
            if recovered is None:
                return artifact.manifest.revision.revision_id
            return canonical_digest(
                {
                    "invocation_id": recovered.invocation_id,
                    "graph_revision": recovered.graph_revision,
                    "product_lock_digest": recovered.product_lock_digest,
                    "root_input_digest": recovered.root_input_digest,
                }
            )

    async def _run_langgraph(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        invocation_id: str,
        record: SelectionRecord,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            await ports.backend.recover_handshake(invocation_id)
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
                start_pins=ports.backend,
            )
            application._started[invocation_id] = StartedInvocation(
                thread_id=invocation_id,
                revision_id=ports.revision_id,
                invocation_id=invocation_id,
                entrypoint=record.entrypoint,
            )
            result = await application.run(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=_context(ports, artifact),
            )
            return result.status

    async def _resume_langgraph(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        invocation_id: str,
        record: SelectionRecord,
        resume: object,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            await ports.backend.recover_handshake(invocation_id)
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
                start_pins=ports.backend,
            )
            application._started[invocation_id] = StartedInvocation(
                thread_id=invocation_id,
                revision_id=ports.revision_id,
                invocation_id=invocation_id,
                entrypoint=record.entrypoint,
            )
            result = await application.resume(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=_context(ports, artifact),
                resume=resume,
            )
            return result.status

    async def _status_langgraph(
        self,
        workspace: ChangeWorkspace,
        composition: Any,
        invocation_id: str,
        record: SelectionRecord,
    ) -> tuple[str, object, tuple[object, ...]]:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
            )
            application._started[invocation_id] = StartedInvocation(
                thread_id=invocation_id,
                revision_id=ports.revision_id,
                invocation_id=invocation_id,
                entrypoint=record.entrypoint,
            )
            result = await application.status(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=_context(ports, artifact),
            )
            snapshot = await _graph_snapshot(artifact, record.entrypoint, invocation_id)
            ports._publish_journal_snapshot()
            return result.status, snapshot, ProductRuntimePorts.last_journal_events()

    async def _pending_interrupt_ids(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        invocation_id: str,
        record: SelectionRecord,
    ) -> tuple[str, ...]:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            snapshot = await _graph_snapshot(artifact, record.entrypoint, invocation_id)
        return tuple(
            str(getattr(item, "id"))
            for item in getattr(snapshot, "interrupts", ())
            if getattr(item, "id", None)
        )


@dataclass(frozen=True, slots=True)
class SimpleRun:
    status: str
    terminal_reason: str | None
    actions: tuple[str, ...]
    projection: object | None


def _context(ports: ProductRuntimePorts, artifact: object) -> AssuranceRuntimeContext:
    revision_id = getattr(
        getattr(getattr(artifact, "manifest", None), "revision", None), "revision_id", ports.revision_id
    )
    return AssuranceRuntimeContext(
        revision_id=str(revision_id),
        fencing_token=1,
        attempt_kernel=ports.kernel,
        secret_resolver=object(),
        workspace_provider=object(),
    )


def _load_input(path: Path, *, entrypoint: str, composition: Any) -> ProductInputV1:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (
        ProductInputV1.model_validate(payload)
        .validate_for_entrypoint(entrypoint)
        .authenticate_against(composition)
    )


def _read_historical_legacy(
    workspace: ChangeWorkspace,
    invocation_id: str,
) -> tuple[InvocationProjection, dict[str, str]]:
    invocation_root = workspace.paths.runtime_root / "invocations" / invocation_id
    envelopes = read_legacy_ledger(invocation_root / "ledger")
    if not envelopes:
        raise ValueError("invocation has no ledger bootstrap")
    event = getattr(envelopes[0], "event", None)
    if not isinstance(event, InvocationStarted):
        raise ValueError("invocation ledger lacks its canonical bootstrap")
    identity = {"root_input_digest": event.root_input_digest}
    published = fold_legacy_events(envelopes)
    write_runtime_projections(
        workspace,
        published,
        envelopes,
        root_input_digest=identity["root_input_digest"],
    )
    return published, identity


def _legacy_invocation_exists(workspace: ChangeWorkspace, invocation_id: str) -> bool:
    lock = workspace.paths.runtime_root / "invocations" / invocation_id / "invocation.lock.json"
    return lock.is_file() and not lock.is_symlink()


def _legacy_lock_digest(workspace: ChangeWorkspace, invocation_id: str) -> str | None:
    lock = workspace.paths.runtime_root / "invocations" / invocation_id / "invocation.lock.json"
    if not lock.is_file() or lock.is_symlink():
        return None
    raw = lock.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    digest = payload.get("digest")
    if isinstance(digest, str) and len(digest) == 64:
        return digest
    from hashlib import sha256

    return sha256(raw).hexdigest()


def _langgraph_db_has_invocation(workspace: ChangeWorkspace, invocation_id: str) -> bool:
    return _langgraph_started_row(workspace, invocation_id) is not None


def _langgraph_started_row(
    workspace: ChangeWorkspace, invocation_id: str
) -> tuple[str, str, str, str] | None:
    db = workspace.paths.langgraph_checkpoints
    if not db.is_file() or db.is_symlink():
        return None
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT invocation_id, graph_revision, product_lock_digest, root_input_digest "
                "FROM assurance_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        connection.close()
    if row is None:
        return None
    return (str(row[0]), str(row[1]), str(row[2]), str(row[3]))


def _langgraph_identity_digest(workspace: ChangeWorkspace, invocation_id: str) -> str | None:
    row = _langgraph_started_row(workspace, invocation_id)
    if row is None:
        return None
    return canonical_digest(
        {
            "invocation_id": row[0],
            "graph_revision": row[1],
            "product_lock_digest": row[2],
            "root_input_digest": row[3],
        }
    )


def _bind_revision(
    workspace: ChangeWorkspace,
    *,
    invocation_id: str,
    runtime: Literal["legacy-v2", "langgraph-v1"],
    composition: Any,
    product_lock: ProductLock,
    build_identity: str,
) -> None:
    registry = RevisionRegistry(workspace)
    if runtime != "langgraph-v1":
        registry.bind(invocation_id, runtime="legacy-v2", revision_id=build_identity)
        return
    current = coexistence_graph_manifest(composition, product_lock).revision
    try:
        bound_runtime, bound_id = registry.revision_for(invocation_id)
    except RevisionRegistryError as error:
        if "missing" not in str(error):
            raise
        if current.product_lock_digest != build_identity:
            raise RevisionRegistryError(
                f"required artifact: graph revision {build_identity} product lock {build_identity}"
            ) from error
        registry.remember(current)
        registry.bind(invocation_id, runtime="langgraph-v1", revision_id=current.revision_id)
        return
    if bound_runtime != "langgraph-v1":
        raise RevisionRegistryError(
            f"required artifact: graph revision {bound_id} product lock {build_identity}"
        )
    recorded = registry.get(bound_id)
    if recorded.revision_id != current.revision_id:
        raise RevisionRegistryError(
            "required artifact: "
            f"graph revision {recorded.revision_id} "
            f"product lock {recorded.product_lock_digest}"
        )


def _assert_langgraph_revision(workspace: ChangeWorkspace, composition: Any, invocation_id: str) -> None:
    product_lock = product_lock_from_composition(composition)
    current = coexistence_graph_manifest(composition, product_lock).revision
    assert_recorded_revision(workspace, invocation_id, current)


async def _graph_snapshot(artifact: object, entrypoint: str, invocation_id: str) -> object:
    graph = getattr(artifact, "entrypoints", {})[entrypoint]
    return await graph.aget_state({"configurable": {"thread_id": invocation_id}})


__all__ = [
    "ENTRYPOINT_AGENT_CONTRACT_IDS",
    "AssuranceProductApplication",
    "LangGraphRuntimeRecord",
    "LegacyRuntimeRecord",
    "ProductBuildArtifacts",
    "RuntimeSelectionError",
    "SelectionCrash",
    "SelectionRecord",
    "backfill_legacy",
    "complete_initialized",
    "load_selection",
    "maybe_crash",
    "parse_resume_file",
    "record_digest",
    "require_initialized",
    "selection_path",
    "write_initializing",
]
