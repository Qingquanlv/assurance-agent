from __future__ import annotations

import asyncio
import json
import sqlite3
import yaml
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from graph_engine.application import AssuranceApplication, StartedInvocation
from graph_engine.application.status import TerminalEnvelope
from graph_engine.boot.graph_revision import GraphBuildManifest
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.lock import ProductLock
from graph_engine.attempts.secret_sources import InvocationRuntimeAuthorization

from assurance_product.binding_builder import build_deployment_wheel
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.invocation_identity import (
    InvocationIdentityRecord,
    RuntimeSelectionError,
    SelectionCrash,
    complete_initialized,
    load_identity,
    maybe_crash,
    record_digest,
    require_initialized,
    write_initializing,
)
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, StatusV1
from assurance_product.product import (
    product_graph_manifest,
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
    finalize_achieved,
    load_persisted_status,
    render_status_from_langgraph,
)
from assurance_intake.contracts.plan import TestFamilyPolicyV1

_TEST_CRASH_AT: str | None = None


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
            "assurance.healing.agent.apply-test-repair.v1",
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
    def __init__(self, lock: ProductLock | Mapping[str, object]) -> None:
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
        manifest = product_graph_manifest(composition, product_lock)
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
        if entrypoint in {"full", "intake"}:
            resource = composition.registries.resources.entries.get(product_input.product_policy.resource_id)
            try:
                policy = yaml.safe_load(resource.content)
                family_policy = TestFamilyPolicyV1.model_validate(policy["test_family_policy"])
            except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
                raise ValueError("authenticated product policy has invalid test_family_policy") from error
            if not set(family_policy.required) <= set(product_input.candidate_test_families):
                raise ValueError("policy required families must be candidates")
            if not set(family_policy.allowed) & set(product_input.candidate_test_families):
                raise ValueError("candidate and policy allowed families must intersect")
            root_input_data["family_policy"] = family_policy.model_dump(mode="json")
        root_input = cast(JSONValue, root_input_data)
        root_input_digest = canonical_digest(cast(JSONValue, root_input_data))
        product_lock = product_lock_from_composition(composition)
        existing = load_identity(workspace, invocation_id)
        if existing is not None:
            product_lock_digest = existing.product_lock_digest
            revision_id = existing.revision_id
            if existing.entrypoint != entrypoint or existing.root_input_digest != root_input_digest:
                raise RuntimeSelectionError("identity record disagrees with the requested identity")
            if existing.phase == "initialized":
                return {
                    "invocation_id": invocation_id,
                    "change_id": change_id,
                    "lock_digest": composition.lock_digest,
                    "composition_digest": composition.digest,
                    "root_input_digest": root_input_digest,
                }
        else:
            product_lock_digest = product_lock.digest
            revision_id = product_graph_manifest(composition, product_lock).revision.revision_id
        initializing = InvocationIdentityRecord(
            schema_version="1",
            phase="initializing",
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=root_input_digest,
            product_lock_digest=product_lock_digest,
            revision_id=revision_id,
        )
        write_initializing(workspace, initializing)
        _bind_revision(
            workspace,
            invocation_id=invocation_id,
            composition=composition,
            product_lock=product_lock,
            product_lock_digest=product_lock_digest,
        )
        asyncio.run(
            self._start_langgraph(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input=cast(Mapping[str, JSONValue], root_input),
                product_lock=product_lock,
            )
        )
        maybe_crash("after_identity")
        complete_initialized(
            workspace,
            InvocationIdentityRecord(
                schema_version="1",
                phase="initialized",
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                root_input_digest=root_input_digest,
                product_lock_digest=product_lock_digest,
                revision_id=revision_id,
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
        existing = load_identity(workspace, invocation_id)
        needs_handshake = existing is None
        needs_finish = existing is not None and existing.phase != "initialized"
        if needs_handshake or needs_finish:
            if entrypoint is None or input_path is None:
                if needs_finish:
                    raise RuntimeSelectionError("only an initialized identity record is resumable")
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
        status = asyncio.run(
            self._run_langgraph(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
                record=record,
            )
        )
        self._terminalize_full_if_achieved(
            workspace=workspace,
            composition=composition,
            authorization=authorization,
            invocation_id=invocation_id,
            change_id=change_id,
            record=record,
            status=status,
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
        resume_payload: object
        _assert_langgraph_revision(workspace, composition, invocation_id)
        if resume_file is not None:
            pending_ids = asyncio.run(
                self._pending_interrupt_ids(
                    workspace=workspace,
                    composition=composition,
                    authorization=authorization,
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
                authorization=authorization,
                invocation_id=invocation_id,
                record=record,
                resume=resume_payload,
            )
        )
        self._terminalize_full_if_achieved(
            workspace=workspace,
            composition=composition,
            authorization=authorization,
            invocation_id=invocation_id,
            change_id=workspace.change_id,
            record=record,
            status=status,
        )
        code, mapped = _LG_EXIT[status]
        return SimpleRun(status=status, terminal_reason=None, actions=(), projection=None), mapped, code

    def _terminalize_full_if_achieved(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        change_id: str,
        record: InvocationIdentityRecord,
        status: str,
    ) -> None:
        if status != "completed" or record.entrypoint != "full":
            return
        observed, snapshot, journal_events = asyncio.run(
            self._status_langgraph(
                workspace,
                composition,
                authorization,
                invocation_id,
                record,
            )
        )
        values = getattr(snapshot, "values", None)
        if observed != "completed" or not isinstance(values, Mapping):
            raise RuntimeSelectionError("terminal full checkpoint is incomplete")
        raw_terminal = values.get("terminal")
        if not isinstance(raw_terminal, Mapping):
            raise RuntimeSelectionError("terminal full checkpoint is missing its terminal envelope")
        terminal = TerminalEnvelope.model_validate(raw_terminal)
        if terminal.status != "completed" or terminal.reason != "achieved":
            raise RuntimeSelectionError("terminal full checkpoint is not achieved")
        raw_families = values.get("selected_test_families")
        if not isinstance(raw_families, list | tuple) or any(
            not isinstance(item, str) for item in raw_families
        ):
            raise ValueError("terminal full snapshot is missing selected test families")
        families = tuple(raw_families)
        allowed_families = ("api", "e2e", "fuzz", "performance")
        if (
            not families
            or len(families) != len(set(families))
            or tuple(item for item in allowed_families if item in families) != families
        ):
            raise ValueError("terminal full snapshot selected test families are not canonical")
        if (
            _persisted_achieved_full_status(
                workspace,
                record,
                invocation_id=invocation_id,
                change_id=change_id,
                families=families,
            )
            is not None
        ):
            return
        rendered = render_status_from_langgraph(
            invocation_id=invocation_id,
            lock_digest=record.product_lock_digest,
            root_input_digest=record.root_input_digest,
            entrypoint=record.entrypoint,
            change_id=change_id,
            status=observed,
            snapshot=snapshot,
            journal_events=journal_events,
        )
        if not rendered.selected_test_families:
            raise ValueError("terminal full snapshot is missing selected test families")
        finalize_achieved(
            workspace.paths.project_root,
            change_id,
            rendered.selected_test_families,
            invocation=rendered,
        )

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
        status_name, snapshot, journal_events = asyncio.run(
            self._status_langgraph(
                workspace,
                composition,
                authorization,
                invocation_id,
                record,
            )
        )
        rendered = render_status_from_langgraph(
            invocation_id=invocation_id,
            lock_digest=record.product_lock_digest,
            root_input_digest=record.root_input_digest,
            entrypoint=record.entrypoint,
            change_id=change_id,
            status=status_name,
            snapshot=snapshot,
            journal_events=journal_events,
        )
        if record.entrypoint != "full" or status_name != "completed":
            return rendered
        values = getattr(snapshot, "values", None)
        raw_terminal = values.get("terminal") if isinstance(values, Mapping) else None
        if not isinstance(raw_terminal, Mapping):
            raise RuntimeSelectionError("terminal full checkpoint is missing its terminal envelope")
        terminal = TerminalEnvelope.model_validate(raw_terminal)
        if terminal.status != "completed" or terminal.reason != "achieved":
            raise RuntimeSelectionError("terminal full checkpoint is not achieved")
        persisted = _persisted_achieved_full_status(
            workspace,
            record,
            invocation_id=invocation_id,
            change_id=change_id,
            families=rendered.selected_test_families,
        )
        if persisted is None:
            raise RuntimeSelectionError("terminal full finalization evidence is absent")
        return persisted

    def lock_show(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
    ) -> dict[str, object]:
        self._resolve_existing(
            workspace,
            invocation_id,
            composition=composition,
            authorization=authorization,
        )
        product_lock = product_lock_from_composition(composition)
        manifest = product_graph_manifest(composition, product_lock)
        return {
            "lock_digest": product_lock.digest,
            "engine_api": product_lock.engine_api,
            "lock": product_lock.model_dump(mode="json"),
            "revision": manifest.revision.model_dump(mode="json"),
        }

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
    ) -> InvocationIdentityRecord:
        record = load_identity(workspace, invocation_id)
        if record is None:
            raise RuntimeSelectionError("invocation evidence is absent")
        if record.phase == "initialized":
            row = _langgraph_started_row(workspace, invocation_id)
            if row is None:
                raise RuntimeSelectionError("langgraph evidence is absent")
            if (
                record.revision_id != row[1]
                or record.product_lock_digest != row[2]
                or record.root_input_digest != row[3]
            ):
                raise RuntimeSelectionError("identity record disagrees with langgraph evidence")
        return record

    def _open_ports(
        self,
        workspace: ChangeWorkspace,
        composition: Any,
        *,
        invocation_id: str,
        authorization: InvocationRuntimeAuthorization,
        entrypoint: str,
    ):
        return ProductRuntimePorts.open(
            workspace,
            composition,
            invocation=invocation_id,
            authorization=authorization,
            reachable_contract_ids=ENTRYPOINT_AGENT_CONTRACT_IDS[entrypoint],
        )

    def _execution_factory(
        self,
        ports: ProductRuntimePorts,
        *,
        invocation_id: str,
        entrypoint: str,
        root_input_digest: str,
    ):
        return ports.execution_factory(
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=root_input_digest,
        )

    def _application(self, ports: ProductRuntimePorts) -> AssuranceApplication:
        return AssuranceApplication(
            lease=ports.backend.lease,
            owner_id="assurance-product",
            start_pins=ports.backend,
        )

    def _remember_started(
        self,
        application: AssuranceApplication,
        ports: ProductRuntimePorts,
        invocation_id: str,
        entrypoint: str,
    ) -> None:
        application._started[invocation_id] = StartedInvocation(
            thread_id=invocation_id,
            revision_id=ports.revision_id,
            invocation_id=invocation_id,
            entrypoint=entrypoint,
        )

    async def _start_langgraph(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        entrypoint: str,
        root_input: Mapping[str, JSONValue],
        product_lock: ProductLock,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        root_input_digest = canonical_digest(dict(root_input))
        async with self._open_ports(
            workspace,
            composition,
            invocation_id=invocation_id,
            authorization=authorization,
            entrypoint=entrypoint,
        ) as ports:
            RevisionRegistry(workspace).remember(product_graph_manifest(composition, product_lock).revision)
            application = self._application(ports)
            started = await application.start(
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=root_input,
                execution_factory=self._execution_factory(
                    ports,
                    invocation_id=invocation_id,
                    entrypoint=entrypoint,
                    root_input_digest=root_input_digest,
                ),
            )
            del started
            maybe_crash("after_identity")
            recovered = await ports.backend.recover_handshake(invocation_id)
            if recovered is None:
                return ports.revision_id
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
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        record: InvocationIdentityRecord,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with self._open_ports(
            workspace,
            composition,
            invocation_id=invocation_id,
            authorization=authorization,
            entrypoint=record.entrypoint,
        ) as ports:
            application = self._application(ports)
            self._remember_started(application, ports, invocation_id, record.entrypoint)
            result = await application.run(
                invocation_id=invocation_id,
                execution_factory=self._execution_factory(
                    ports,
                    invocation_id=invocation_id,
                    entrypoint=record.entrypoint,
                    root_input_digest=record.root_input_digest,
                ),
            )
            return result.status

    async def _resume_langgraph(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        record: InvocationIdentityRecord,
        resume: object,
    ) -> str:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with self._open_ports(
            workspace,
            composition,
            invocation_id=invocation_id,
            authorization=authorization,
            entrypoint=record.entrypoint,
        ) as ports:
            application = self._application(ports)
            self._remember_started(application, ports, invocation_id, record.entrypoint)
            result = await application.resume(
                invocation_id=invocation_id,
                execution_factory=self._execution_factory(
                    ports,
                    invocation_id=invocation_id,
                    entrypoint=record.entrypoint,
                    root_input_digest=record.root_input_digest,
                ),
                resume=resume,
            )
            return result.status

    async def _status_langgraph(
        self,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        record: InvocationIdentityRecord,
    ) -> tuple[str, object, tuple[object, ...]]:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with self._open_ports(
            workspace,
            composition,
            invocation_id=invocation_id,
            authorization=authorization,
            entrypoint=record.entrypoint,
        ) as ports:
            bound = await ports.read_only_execution(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            application = AssuranceApplication(
                lease=ports.backend.lease,
                owner_id="assurance-product",
            )
            self._remember_started(application, ports, invocation_id, record.entrypoint)
            result = await application.status(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                runtime_context=bound.runtime_context,
            )
            snapshot = await _graph_snapshot(bound.artifact, record.entrypoint, invocation_id)
            await ports._publish_journal_snapshot()
            return result.status, snapshot, ProductRuntimePorts.last_journal_events()

    async def _pending_interrupt_ids(
        self,
        *,
        workspace: ChangeWorkspace,
        composition: Any,
        authorization: InvocationRuntimeAuthorization,
        invocation_id: str,
        record: InvocationIdentityRecord,
    ) -> tuple[str, ...]:
        _assert_langgraph_revision(workspace, composition, invocation_id)
        async with self._open_ports(
            workspace,
            composition,
            invocation_id=invocation_id,
            authorization=authorization,
            entrypoint=record.entrypoint,
        ) as ports:
            bound = await ports.read_only_execution(
                invocation_id=invocation_id,
                root_input_digest=record.root_input_digest,
            )
            snapshot = await _graph_snapshot(bound.artifact, record.entrypoint, invocation_id)
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


def _persisted_achieved_full_status(
    workspace: ChangeWorkspace,
    record: InvocationIdentityRecord,
    *,
    invocation_id: str,
    change_id: str,
    families: tuple[str, ...],
) -> StatusV1 | None:
    persisted = load_persisted_status(workspace)
    if persisted is None:
        return None
    expected_identity = (
        invocation_id,
        record.product_lock_digest,
        record.root_input_digest,
        record.entrypoint,
        change_id,
    )
    actual_identity = (
        persisted.invocation_id,
        persisted.lock_digest,
        persisted.root_input_digest,
        persisted.entrypoint,
        persisted.change.change_id,
    )
    if actual_identity != expected_identity:
        return None
    if (
        persisted.status != "completed"
        or persisted.change.state != "achieved"
        or persisted.selected_test_families != families
    ):
        raise RuntimeSelectionError("persisted terminal status is not an achieved full result")
    return persisted


def _load_input(path: Path, *, entrypoint: str, composition: Any) -> ProductInputV1:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (
        ProductInputV1.model_validate(payload)
        .validate_for_entrypoint(entrypoint)
        .authenticate_against(composition)
    )


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


def _bind_revision(
    workspace: ChangeWorkspace,
    *,
    invocation_id: str,
    composition: Any,
    product_lock: ProductLock,
    product_lock_digest: str,
) -> None:
    registry = RevisionRegistry(workspace)
    current = product_graph_manifest(composition, product_lock).revision
    try:
        bound_id = registry.revision_for(invocation_id)
    except RevisionRegistryError as error:
        if "missing" not in str(error):
            raise
        if current.product_lock_digest != product_lock_digest:
            raise RevisionRegistryError(
                f"required artifact: graph revision {product_lock_digest} product lock {product_lock_digest}"
            ) from error
        registry.remember(current)
        registry.bind(invocation_id, current.revision_id)
        return
    recorded = registry.get(bound_id)
    if recorded.revision_id != current.revision_id:
        raise RevisionRegistryError(
            "required artifact: "
            f"graph revision {recorded.revision_id} "
            f"product lock {recorded.product_lock_digest}"
        )


def _assert_langgraph_revision(workspace: ChangeWorkspace, composition: Any, invocation_id: str) -> None:
    product_lock = product_lock_from_composition(composition)
    current = product_graph_manifest(composition, product_lock).revision
    assert_recorded_revision(workspace, invocation_id, current)


async def _graph_snapshot(artifact: object, entrypoint: str, invocation_id: str) -> object:
    graph = getattr(artifact, "entrypoints", {})[entrypoint]
    return await graph.aget_state({"configurable": {"thread_id": invocation_id}})


__all__ = [
    "ENTRYPOINT_AGENT_CONTRACT_IDS",
    "AssuranceProductApplication",
    "InvocationIdentityRecord",
    "ProductBuildArtifacts",
    "RuntimeSelectionError",
    "SelectionCrash",
    "complete_initialized",
    "load_identity",
    "maybe_crash",
    "parse_resume_file",
    "record_digest",
    "require_initialized",
    "write_initializing",
]
