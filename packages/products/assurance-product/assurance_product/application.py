from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_runtime_contracts import AgentRuntimeCapabilities
from agent_runtime_contracts.plugin_kit import negotiate_provider_schema
from graph_engine.application import AssuranceApplication, AssuranceRuntimeContext
from graph_engine.boot.graph_revision import GraphBuildManifest
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.lock import InvocationLock, ProductLock
from graph_engine.runtime.engine import Engine, RunResult
from graph_engine.runtime.events import InvocationStarted
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization

from assurance_product.binding_builder import build_deployment_wheel
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.export import publish_achieved, select_publish_change
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, StatusV1
from assurance_product.product import (
    GraphAuditResult,
    coexistence_graph_manifest,
    product_lock_from_composition,
    reject_organization_overrides,
)
from assurance_product.revision_registry import RevisionRegistry
from assurance_product.runtime_ports import ProductRuntimePorts
from assurance_product.runtime_selection import (
    LangGraphRuntimeRecord,
    LegacyRuntimeRecord,
    RuntimeSelectionError,
    SelectionRecord,
    backfill_legacy,
    complete_initialized,
    load_selection,
    maybe_crash,
    require_initialized,
    select_runtime,
    write_initializing,
)
from assurance_product.status import (
    archive_published,
    render_status,
    render_status_from_langgraph,
    write_runtime_projections,
)


_LG_EXIT = {
    "completed": (0, "completed"),
    "running": (20, "running"),
    "blocked": (20, "blocked"),
    "stopped": (20, "stopped"),
    "interrupted": (30, "interrupted"),
    "failed": (40, "failed"),
}


class CoexistenceBuildArtifacts:
    def __init__(
        self,
        invocation_lock: InvocationLock | Mapping[str, object],
        product_lock: ProductLock | Mapping[str, object],
        manifest: GraphBuildManifest | Mapping[str, object],
    ) -> None:
        self.invocation_lock = _LockView(invocation_lock)
        self.product_lock = _LockView(product_lock)
        self.manifest = _ManifestView(manifest)

    @classmethod
    def model_validate(cls, data: Mapping[str, object]) -> CoexistenceBuildArtifacts:
        return cls(
            invocation_lock=cast(Mapping[str, object], data["invocation_lock"]),
            product_lock=cast(Mapping[str, object], data["product_lock"]),
            manifest=cast(Mapping[str, object], data["manifest"]),
        )

    def model_dump(self) -> dict[str, object]:
        return {
            "invocation_lock": self.invocation_lock.model_dump(mode="json"),
            "product_lock": self.product_lock.model_dump(mode="json"),
            "manifest": self.manifest.model_dump(mode="json"),
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
            self.revision = revision if not isinstance(revision, Mapping) else _RevisionView(revision)
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


def assert_structured_output_capability(
    composition: object,
    *,
    requires_provider_schema: bool | None = None,
) -> None:
    required = (
        _contracts_require_provider_schema(composition)
        if requires_provider_schema is None
        else requires_provider_schema
    )
    negotiate_provider_schema(
        required=required,
        capabilities=_advertised_binding_capabilities(composition),
    )


class AssuranceProductApplication:
    def __init__(self, engine_factory: Callable[..., Engine] | None = None) -> None:
        self._engine_factory = engine_factory

    def compile(
        self,
        composition: Any,
        audit: GraphAuditResult,
        *,
        product: str,
        config_tree: str,
    ) -> dict[str, object]:
        reject_organization_overrides(Path(config_tree))
        product_lock = product_lock_from_composition(composition)
        manifest = coexistence_graph_manifest(composition, product_lock)
        bundle = CoexistenceBuildArtifacts(composition.lock, product_lock, manifest)
        dumped = bundle.model_dump()
        return {
            "lock_digest": composition.lock_digest,
            "composition_digest": composition.digest,
            "workflow_digest": composition.workflow.digest,
            "product": product,
            "engine_api": composition.lock.engine_api,
            "entrypoints": sorted(composition.workflow.entrypoints),
            "audit": audit.model_dump(mode="json"),
            "product_lock_digest": product_lock.digest,
            "revision_id": manifest.revision.revision_id,
            "coexistence": dumped,
        }

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
        root_input = cast(JSONValue, product_input.model_dump(mode="json"))
        root_input_digest = canonical_digest(cast(JSONValue, dict(root_input)))
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
            runtime = select_runtime(entrypoint)
            build_identity = composition.lock.digest if runtime == "legacy-v2" else product_lock.digest
        initializing: SelectionRecord
        if runtime == "legacy-v2":
            initializing = LegacyRuntimeRecord(
                phase="initializing",
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input_digest=root_input_digest,
                build_identity=build_identity,
            )
        else:
            initializing = LangGraphRuntimeRecord(
                phase="initializing",
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input_digest=root_input_digest,
                build_identity=build_identity,
            )
        write_initializing(workspace, initializing)
        if runtime == "legacy-v2":
            identity_digest = self._start_legacy(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input=root_input,
            )
            maybe_crash("after_identity")
            complete_initialized(
                workspace,
                LegacyRuntimeRecord(
                    phase="initialized",
                    invocation_id=invocation_id,
                    entrypoint=entrypoint,
                    root_input_digest=root_input_digest,
                    build_identity=build_identity,
                    identity_digest=identity_digest,
                ),
            )
        else:
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
    ) -> tuple[object, str, int]:
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
            return self._run_legacy(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
            )
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
    ) -> tuple[object, str, int]:
        record = self._resolve_existing(
            workspace,
            invocation_id,
            composition=composition,
            authorization=authorization,
        )
        require_initialized(record)
        if record.runtime == "legacy-v2":
            if resume_file is not None:
                payload = parse_resume_file(resume_file)
                if isinstance(payload, dict) and "action" in payload:
                    action = str(payload["action"])
                    reason = str(payload.get("reason") or "resumed")
            if action is None or reason is None:
                raise ValueError("legacy resume requires --action and --reason")
            return self._resume_legacy(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
                action=action,
                reason=reason,
            )
        resume_payload: object
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
            projection, identity = self._legacy_projection(
                workspace, composition, authorization, invocation_id
            )
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
            return {
                "lock_digest": composition.lock_digest,
                "engine_api": composition.lock.engine_api,
                "lock": composition.lock.model_dump(mode="json"),
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
        projection, identity = self._legacy_projection(workspace, composition, authorization, invocation_id)
        return backfill_legacy(
            workspace,
            invocation_id=invocation_id,
            entrypoint=projection.entrypoint or "",
            root_input_digest=identity["root_input_digest"],
            build_identity=composition.lock.digest,
            identity_digest=composition.lock.digest,
        )

    def _start_legacy(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        entrypoint: str,
        root_input: JSONValue,
    ) -> str:
        from graph_engine.runtime.seed import empty_invocation_seed

        factory = self._engine_factory
        if factory is None:
            from graph_engine.runtime.engine import Engine as ProductionEngine

            factory = ProductionEngine.production
        with factory(workspace.paths.runtime_root, authorization) as engine:
            if engine.invocation_exists(invocation_id):
                maybe_crash("after_identity")
                return composition.lock.digest
            with engine.start(
                composition,
                entrypoint=entrypoint,
                invocation_id=invocation_id,
                seed=empty_invocation_seed(root_input=root_input),
                authorization=authorization,
                workspace_binding=workspace.runtime_binding(),
            ) as handle:
                _publish_legacy(workspace, handle.invocation_root)
        maybe_crash("after_identity")
        return composition.lock.digest

    def _run_legacy(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
    ) -> tuple[RunResult, str, int]:
        from graph_engine.runtime.driver import acquire_invocation

        factory = self._engine_factory
        if factory is None:
            from graph_engine.runtime.engine import Engine as ProductionEngine

            factory = ProductionEngine.production
        with factory(workspace.paths.runtime_root, authorization) as engine:
            with acquire_invocation(
                engine,
                composition,
                invocation_id=invocation_id,
                authorization=authorization,
                workspace_binding=workspace.runtime_binding(),
                start=None,
            ) as handle:
                result = engine.run_until_blocked(handle)
                _publish_legacy(workspace, handle.invocation_root, result.projection)
        mapped = {
            "succeeded": (0, "completed"),
            "stopped": (20, "stopped"),
            "interrupted": (30, "interrupted"),
            "failed": (40, "failed"),
        }
        code, name = mapped[result.status]
        return result, name, code

    def _resume_legacy(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        action: str,
        reason: str,
    ) -> tuple[RunResult, str, int]:
        from graph_engine.runtime.driver import acquire_invocation

        factory = self._engine_factory
        if factory is None:
            from graph_engine.runtime.engine import Engine as ProductionEngine

            factory = ProductionEngine.production
        with factory(workspace.paths.runtime_root, authorization) as engine:
            with acquire_invocation(
                engine,
                composition,
                invocation_id=invocation_id,
                authorization=authorization,
                workspace_binding=workspace.runtime_binding(),
                start=None,
            ) as opened:
                resumed = engine.resume(opened, action=action, payload={"reason": reason})
                try:
                    result = engine.run_until_blocked(resumed)
                    _publish_legacy(workspace, resumed.invocation_root, result.projection)
                finally:
                    resumed.close()
        mapped = {
            "succeeded": (0, "completed"),
            "stopped": (20, "stopped"),
            "interrupted": (30, "interrupted"),
            "failed": (40, "failed"),
        }
        code, name = mapped[result.status]
        return result, name, code

    def _legacy_projection(
        self,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
    ) -> tuple[InvocationProjection, dict[str, str]]:
        from graph_engine.runtime.driver import acquire_invocation

        factory = self._engine_factory
        if factory is None:
            from graph_engine.runtime.engine import Engine as ProductionEngine

            factory = ProductionEngine.production
        with factory(workspace.paths.runtime_root, authorization) as engine:
            with acquire_invocation(
                engine,
                composition,
                invocation_id=invocation_id,
                authorization=authorization,
                workspace_binding=workspace.runtime_binding(),
                start=None,
            ) as handle:
                return _publish_legacy(workspace, handle.invocation_root)

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
        if ProductRuntimePorts.test_kernel_resolutions is None:
            assert_structured_output_capability(composition)
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
            application._started[invocation_id] = type(
                "Started",
                (),
                {"entrypoint": record.entrypoint, "invocation_id": invocation_id, "thread_id": invocation_id},
            )()
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
        if ProductRuntimePorts.test_kernel_resolutions is None:
            assert_structured_output_capability(composition)
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
            application._started[invocation_id] = type(
                "Started",
                (),
                {"entrypoint": record.entrypoint, "invocation_id": invocation_id, "thread_id": invocation_id},
            )()
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
        async with ProductRuntimePorts.open(workspace, composition) as ports:
            artifact = await ports.compile_roots(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
            )
            application._started[invocation_id] = type(
                "Started",
                (),
                {"entrypoint": record.entrypoint, "invocation_id": invocation_id, "thread_id": invocation_id},
            )()
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


def _publish_legacy(
    workspace: ChangeWorkspace,
    invocation_root: Path,
    projection: InvocationProjection | None = None,
) -> tuple[InvocationProjection, dict[str, str]]:
    envelopes = Ledger(invocation_root / "ledger").read_all()
    if not envelopes:
        raise ValueError("invocation has no ledger bootstrap")
    event = getattr(envelopes[0], "event", None)
    if not isinstance(event, InvocationStarted):
        raise ValueError("invocation ledger lacks its canonical bootstrap")
    identity = {"root_input_digest": event.root_input_digest}
    published = fold_events(envelopes) if projection is None else projection
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


def _advertised_binding_capabilities(composition: object) -> AgentRuntimeCapabilities:
    from assurance_product.source_catalog import adapter_for_entrypoint

    source = getattr(getattr(composition, "manifest", None), "source", None)
    name = getattr(source, "entrypoint_name", None)
    adapter = adapter_for_entrypoint(name) if isinstance(name, str) else "opencode"
    if adapter == "opencode":
        from agent_runtime_opencode.observation import advertised_runtime_capabilities

        return advertised_runtime_capabilities()
    from agent_runtime_cursor.plugin import CursorPlugin

    return CursorPlugin.spec.capabilities


def _contracts_require_provider_schema(composition: object) -> bool:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    del composition
    return any(contract.requires_provider_schema for contract in all_feature_agent_contracts().values())


async def _graph_snapshot(artifact: object, entrypoint: str, invocation_id: str) -> object:
    graph = getattr(artifact, "entrypoints", {})[entrypoint]
    return await graph.aget_state({"configurable": {"thread_id": invocation_id}})


__all__ = [
    "AssuranceProductApplication",
    "CoexistenceBuildArtifacts",
    "assert_structured_output_capability",
    "parse_resume_file",
]
