"""Durable dispatch evidence and a stop-only bridge to the installed host.

Retained calls contain authorized secret handles, never resolved secret bytes.
Cancellation reconstructs the original envelope; prepare/finalize are not run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from graph_engine.attempts.activity import JournalBackedTaskActivityPort
from graph_engine.attempts.events import ResourcesAuthorized
from graph_engine.attempts.host_protocol import TaskHostCancelCall, TaskHostExecuteCall
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.plugin_api import DirectoryIdentity
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from assurance_product.sqlite_checkpointer import AssuranceSqliteBackend
from assurance_product.worker_lifecycle import ExecutionConflict, current_owner, update_owner


def retain_stop_authority(owner: Any, workspace: Any, composition: Any, authorization: Any) -> None:
    """Retain installed source selectors and secret locators on the admitted owner."""
    from dataclasses import asdict
    from graph_engine.frozen_json import thaw_json
    from assurance_product.product import product_graph_revision, product_lock_from_composition

    deployment = [
        item
        for item in composition.lock.plugins
        if item.source.identity.get("entrypoint_name") == "deployment"
    ]
    config = [item for item in composition.lock.plugins if item.source.kind == "config_tree"]
    if len(deployment) != 1 or len(config) != 1:
        raise ExecutionConflict(
            "foreground stop requires one authenticated deployment and configuration source"
        )
    source = thaw_json(deployment[0].source.identity)
    lock = product_lock_from_composition(composition)
    authority = {
        "change_id": workspace.change_id,
        "product": "assurance-opencode",
        "binding_dist": source["distribution"],
        "binding_entrypoint": source["entrypoint_name"],
        "binding_declaration": source["declaration_path"],
        "config_tree": str(config[0].source.identity["root"]),
        "authorization": asdict(authorization),
        "product_lock_digest": lock.digest,
        "graph_revision": product_graph_revision(composition, lock).revision_id,
    }
    authority = json.loads(json.dumps(authority))

    def retain(record: dict[str, Any]) -> None:
        prior = record.get("stop_authority")
        if prior is not None and prior != authority:
            raise ExecutionConflict("nested execution cancellation authority differs")
        record["stop_authority"] = authority

    update_owner(owner, retain)
    owner.stop_authority_digest = canonical_digest(authority)


class RetainedHost:
    def __init__(self, inner: Any, backend: AssuranceSqliteBackend, journal: AttemptJournalPort) -> None:
        self._inner = inner
        self._backend = backend
        self._journal = journal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def execute(self, call: TaskHostExecuteCall) -> Any:
        owner = current_owner()
        if owner is None or owner.invocation != call.identity.invocation_id:
            raise ExecutionConflict("production dispatch requires the current Invocation owner")
        if owner.stop_authority_digest is None:
            raise ExecutionConflict("production dispatch requires retained cancellation authority")
        snapshot = await self._journal.load(AttemptKey(digest=call.identity.attempt_key_digest))
        if (
            snapshot is None
            or snapshot.invocation_id != owner.invocation
            or snapshot.fencing_token != call.identity.fencing_token
            or snapshot.authorization_id != call.identity.authorization_id
            or snapshot.graph_revision != call.identity.graph_revision
        ):
            raise ExecutionConflict("retained host call disagrees with authenticated Attempt ownership")
        payload = call.model_dump(mode="json")
        digest = canonical_digest(cast(JSONValue, payload))
        async with self._backend.store._lock:
            await self._backend._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._backend._conn.execute(
                    "INSERT INTO assurance_host_calls (call_digest, owner_nonce, attempt_key_digest, payload, stop_authority_digest) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(call_digest) DO NOTHING",
                    (
                        digest,
                        owner.nonce,
                        call.identity.attempt_key_digest,
                        canonical_json_bytes(cast(JSONValue, payload)),
                        owner.stop_authority_digest,
                    ),
                )
                await self._backend._conn.commit()
            except BaseException:
                await self._backend._conn.rollback()
                raise
        if call.identity.phase == "runtime":

            def registered(record: dict[str, Any]) -> None:
                if digest not in record["calls"]:
                    record["calls"].append(digest)

            update_owner(owner, registered)
        result = await self._inner.execute(call)
        # Provider uncertainty is an unresolved external execution, even if a
        # trusted adapter reports it as an external_effect failure.
        outcome = result.outcome
        confirmed = outcome is not None and not (
            outcome.failure is not None and outcome.failure.kind == "external_effect"
        )
        if confirmed:
            async with self._backend.store._lock:
                await self._backend._conn.execute(
                    "UPDATE assurance_host_calls SET confirmed = 1 WHERE call_digest = ?", (digest,)
                )
                await self._backend._conn.commit()
            if call.identity.phase == "runtime":
                update_owner(
                    owner,
                    lambda record: record["calls"].remove(digest) if digest in record["calls"] else None,
                )
        return result


async def confirm_owned_calls(owner: dict[str, Any]) -> bool:
    """After verified local exit, cancel only this owner's retained activity calls."""
    from pathlib import Path
    from assurance_product.bootstrap.status import read_run_manifest
    from assurance_product.bootstrap.spec import load_run_spec
    from assurance_product.bootstrap.driver import _secret_arg
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import _authorize_secrets, _resolve_and_audit
    from assurance_product.runtime_ports import ProductRuntimePorts
    from assurance_product.sqlite_resource_authorization import SqliteResourceAuthorizationStore

    from assurance_product.worker_lifecycle import validated_stop_checkpoint

    db = validated_stop_checkpoint(owner)
    if db is None:
        return True
    import sqlite3

    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        saved = connection.execute(
            "SELECT 1 FROM assurance_host_calls WHERE owner_nonce = ? LIMIT 1", (owner["nonce"],)
        ).fetchone()
        if saved is None:
            return not owner["calls"]
    finally:
        connection.close()
    authority = owner.get("stop_authority")
    if authority is not None:
        from graph_engine.attempts.secret_sources import InvocationRuntimeAuthorization, SecretSourceBinding
        from assurance_product.product import product_graph_revision, product_lock_from_composition

        composition, _ = _resolve_and_audit(
            product=authority["product"],
            binding_dist=authority["binding_dist"],
            binding_entrypoint=authority["binding_entrypoint"],
            binding_declaration=authority["binding_declaration"],
            config_tree=authority["config_tree"],
        )
        raw = authority["authorization"]
        authorization = InvocationRuntimeAuthorization(
            schema_version=raw["schema_version"],
            secret_sources=tuple(SecretSourceBinding(**item) for item in raw["secret_sources"]),
            digest=raw["digest"],
        )
        lock = product_lock_from_composition(composition)
        if (
            lock.digest != authority["product_lock_digest"]
            or product_graph_revision(composition, lock).revision_id != authority["graph_revision"]
        ):
            raise ExecutionConflict("foreground cancellation runtime identity drifted")
        workspace = ChangeWorkspace.open(Path(owner["workspace"]), authority["change_id"])
    else:
        run_value = owner.get("run_dir")
        if run_value is None:
            return not owner["calls"]
        run_dir = Path(run_value)
        manifest = read_run_manifest(run_dir)
        if manifest.get("invocation_id") != owner["invocation"] or Path(
            str(manifest["project_dir"])
        ).resolve() != Path(owner["workspace"]):
            raise ExecutionConflict("stop manifest disagrees with recorded worker owner")
        workspace = ChangeWorkspace.open(
            Path(owner["workspace"]), str(manifest.get("change_id") or owner["invocation"])
        )
        composition, _ = _resolve_and_audit(
            product=str(manifest["product"]),
            binding_dist=str(manifest["binding_dist"]),
            binding_entrypoint="deployment",
            binding_declaration=str(manifest["binding_declaration"]),
            config_tree=str(manifest["config_tree"]),
        )
        spec = load_run_spec(run_dir / "run-spec.effective.yaml")
        authorization = _authorize_secrets(composition, (_secret_arg(spec),))
    async with ProductRuntimePorts.open(
        workspace, composition, owner["invocation"], authorization=authorization
    ) as ports:
        backend = ports.backend
        async with backend.store._lock:
            cursor = await backend._conn.execute(
                "SELECT call_digest, attempt_key_digest, payload, confirmed, stop_authority_digest FROM assurance_host_calls WHERE owner_nonce = ?",
                (owner["nonce"],),
            )
            rows = await cursor.fetchall()
        saved = {str(row[0]) for row in rows}
        if not set(owner["calls"]).issubset(saved):
            raise ExecutionConflict("legacy in-flight activity has no retained authenticated envelope")
        lease = await backend.lease.acquire(owner["invocation"], owner_id="stop-" + owner["nonce"])
        try:
            arbiter = ResourceArbiter(SqliteResourceAuthorizationStore(backend))
            for digest, saved_key, payload, confirmed, authority_digest in rows:
                if authority_digest is not None and (
                    authority is None or canonical_digest(authority) != authority_digest
                ):
                    raise ExecutionConflict("retained cancellation authority drifted")
                raw = json.loads(bytes(payload))
                if canonical_digest(raw) != digest:
                    raise ExecutionConflict("retained call digest drifted")
                call = TaskHostExecuteCall.model_validate(raw)
                identity = call.identity
                if identity.attempt_key_digest != str(saved_key):
                    raise ExecutionConflict("retained call key column drifted from envelope")
                if (
                    identity.invocation_id != owner["invocation"]
                    or call.attempt_root.project_root_identity
                    != DirectoryIdentity.capture(Path(owner["workspace"]))
                    or identity.graph_revision != ports.revision_id
                    or identity.product_lock_digest != ports.product_lock_digest
                    or not set(call.authorized_secret_handles).issubset(authorization.authorized_handles)
                ):
                    raise ExecutionConflict("retained call pinned runtime identity drifted")
                key = AttemptKey(digest=identity.attempt_key_digest)
                snapshot = await ports.attempt_journal.load(key)
                if (
                    snapshot is None
                    or snapshot.invocation_id != owner["invocation"]
                    or snapshot.authorization_id != identity.authorization_id
                    or snapshot.graph_revision != identity.graph_revision
                ):
                    raise ExecutionConflict("retained call disagrees with Attempt journal")
                if identity.phase != "runtime" or confirmed:
                    continue
                # Authenticate old receipts against the exact old identity before
                # advancing the fence; they are never returned as business results.
                receipts = ports.host.read_terminal_receipts(identity)  # type: ignore[attr-defined]
                if any(receipt.outcome.status in {"succeeded", "stopped"} for receipt in receipts):
                    await _confirm(backend, str(digest))
                    continue
                if snapshot.terminal is not None or snapshot.released:
                    return False
                await arbiter.adopt(key, fencing_token=lease.fencing_token)
                if snapshot.fencing_token != lease.fencing_token:
                    snapshot = await ports.attempt_journal.append(
                        key,
                        (ResourcesAuthorized(authorization_id=identity.authorization_id),),
                        expected_revision=snapshot.revision,
                        fencing_token=lease.fencing_token,
                    )
                bound_identity = identity.model_copy(
                    update={"operation": "cancel", "fencing_token": lease.fencing_token}
                )
                rpc = call.activity_rpc.model_copy(update={"fencing_token": lease.fencing_token})

                async def assert_fence() -> None:
                    await backend.lease.assert_current(owner["invocation"], lease.fencing_token)

                activity = JournalBackedTaskActivityPort(
                    journal=ports.attempt_journal,
                    attempt_key=key,
                    identity=rpc,
                    workspace_identity=call.attempt_root.workspace_identity,
                    assert_live_fence=assert_fence,
                    owner_loop=asyncio.get_running_loop(),
                    remaining_deadline=30,
                    expected_request_digest=canonical_digest(
                        cast(JSONValue, call.request.model_dump(mode="json"))
                    ),
                    expected_product_lock_digest=ports.product_lock_digest,
                    expected_handler_id=call.request.capability_id,
                )
                cancel = TaskHostCancelCall(
                    identity=bound_identity,
                    capability_id=call.capability_id,
                    capability_entrypoint=call.capability_entrypoint,
                    request=call.request,
                    attempt_root=call.attempt_root,
                    activity_rpc=rpc,
                    authorized_secret_handles=call.authorized_secret_handles,
                    timeout_seconds=30,
                    activity=await activity._load_snapshot(),
                )
                result = await ports.host.cancel(cancel)  # type: ignore[attr-defined]
                if result.cancel_result is None or result.cancel_result.status != "terminal":
                    return False
                await _confirm(backend, str(digest))
            return True
        finally:
            await backend.lease.release(lease)


async def _confirm(backend: AssuranceSqliteBackend, digest: str) -> None:
    async with backend.store._lock:
        await backend._conn.execute(
            "UPDATE assurance_host_calls SET confirmed = 1 WHERE call_digest = ?", (digest,)
        )
        await backend._conn.commit()
