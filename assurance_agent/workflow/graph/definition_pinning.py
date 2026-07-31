"""Pin and inherit replay definition bindings for graph invocations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from assurance_agent.artifacts.policy import (
    PolicyError,
    PolicyOrigin,
    PolicySnapshot,
    load_policy_snapshot_bytes,
)
from assurance_agent.verification.profile_manifest import (
    assurance_profile_bytes,
    assurance_profile_digest,
    assurance_profile_snapshot_relpath,
    parse_assurance_profile_snapshot,
)
from assurance_agent.workflow.core.progression import ProgressionTxn
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.models import CompiledWorkflow, GraphProjection
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest

_POLICY_LOGICAL_PATH = "project:.aa/policy.yaml"
_SCHEMA_DIR = ".graph-runtime/schemas"
_CONTRACT_DIR = ".graph-runtime/contracts"
_CATALOG_DIR = ".graph-runtime/ingest-catalogs"
_POLICY_DIR = ".graph-runtime/policies"


@dataclass(frozen=True, slots=True)
class InvocationDefinitionBinding:
    event_schema_version: int
    policy_digest: str
    policy_origin: PolicyOrigin
    policy_bytes: bytes
    gate_semantics_digest: str
    assurance_profile_digest: str
    assurance_profile_bytes: bytes | None


def policy_snapshot_relpath(policy_digest: str) -> str:
    return f"{_POLICY_DIR}/{policy_digest}.json"


def is_definition_binding_replayable(projection: GraphProjection) -> bool:
    if projection.event_schema_version < 4:
        return False
    return bool(
        projection.policy_digest
        and projection.policy_origin
        and projection.gate_semantics_digest
        and projection.assurance_profile_digest
    )


def bind_root_definitions(
    *,
    store: TreeStore,
    root_tree_id: str,
    event_schema_version: int = 4,
) -> InvocationDefinitionBinding:
    origin = f"tree {root_tree_id}:{_POLICY_LOGICAL_PATH}"
    try:
        data = store.read_bytes(root_tree_id, _POLICY_LOGICAL_PATH)
    except FileNotFoundError:
        snap = load_policy_snapshot_bytes(None, origin="packaged_default")
    except OSError as exc:
        raise PolicyError(f"cannot read {origin}: {exc}") from exc
    else:
        snap = load_policy_snapshot_bytes(data, origin="project")
    return _binding_from_snapshot(snap, event_schema_version=event_schema_version)


def inherit_child_definitions(
    *,
    parent: GraphProjection,
    change_dir: Path,
) -> InvocationDefinitionBinding:
    if not parent.policy_digest:
        raise PolicyError("parent invocation has no pinned policy digest")
    profile_bytes: bytes | None = None
    if parent.event_schema_version >= 5:
        profile_bytes = _read_pinned_profile_bytes(change_dir, parent.assurance_profile_digest)
    binding = InvocationDefinitionBinding(
        event_schema_version=parent.event_schema_version,
        policy_digest=parent.policy_digest,
        policy_origin=_coerce_policy_origin(parent.policy_origin),
        policy_bytes=_read_pinned_policy_bytes(change_dir, parent.policy_digest),
        gate_semantics_digest=parent.gate_semantics_digest,
        assurance_profile_digest=parent.assurance_profile_digest,
        assurance_profile_bytes=profile_bytes,
    )
    verify_pinned_definitions(binding, change_dir)
    return binding


def stage_pinned_definitions(
    txn: ProgressionTxn,
    compiled: CompiledWorkflow,
    binding: InvocationDefinitionBinding,
    *,
    contracts: ExecutionContractCatalog,
) -> None:
    schema_bytes = (
        json.dumps(
            compiled.schema.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    txn.write_runtime_file_once(f"{_SCHEMA_DIR}/{compiled.digest}.json", schema_bytes)

    catalog = validate_catalog_runtime()
    catalog_bytes = (
        json.dumps(
            catalog.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    txn.write_runtime_file_once(f"{_CATALOG_DIR}/{catalog.digest}.json", catalog_bytes)

    for target, digest in sorted(compiled.contract_digests.items()):
        contract = contracts.contracts.get(target)
        if contract is None:
            continue
        payload = (
            json.dumps(
                contract.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        txn.write_runtime_file_once(f"{_CONTRACT_DIR}/{digest}.json", payload)

    txn.write_runtime_file_once(policy_snapshot_relpath(binding.policy_digest), binding.policy_bytes)
    if binding.assurance_profile_bytes is not None:
        txn.write_runtime_file_once(
            assurance_profile_snapshot_relpath(binding.assurance_profile_digest),
            binding.assurance_profile_bytes,
        )


def verify_pinned_definitions(binding: InvocationDefinitionBinding, change_dir: Path) -> None:
    if not (
        binding.policy_digest
        and binding.policy_origin
        and binding.gate_semantics_digest
        and binding.assurance_profile_digest
    ):
        raise PolicyError("definition binding is incomplete for replay")
    _read_pinned_policy_bytes(change_dir, binding.policy_digest)
    if binding.event_schema_version >= 5:
        if binding.assurance_profile_bytes is None:
            raise PolicyError("v5 definition binding requires assurance profile snapshot bytes")
        pinned = _read_pinned_profile_bytes(change_dir, binding.assurance_profile_digest)
        if pinned != binding.assurance_profile_bytes:
            raise PolicyError("assurance profile snapshot bytes do not match binding")


def _binding_from_snapshot(snap: PolicySnapshot, *, event_schema_version: int) -> InvocationDefinitionBinding:
    profile_data = assurance_profile_bytes() if event_schema_version >= 5 else None
    return InvocationDefinitionBinding(
        event_schema_version=event_schema_version,
        policy_digest=snap.digest,
        policy_origin=snap.origin,
        policy_bytes=snap.canonical_bytes,
        gate_semantics_digest=gate_semantics_digest(),
        assurance_profile_digest=assurance_profile_digest(),
        assurance_profile_bytes=profile_data,
    )


def _coerce_policy_origin(origin: str) -> PolicyOrigin:
    if origin == "project":
        return "project"
    if origin == "packaged_default":
        return "packaged_default"
    raise PolicyError(f"unknown policy origin {origin!r}")


def _read_pinned_policy_bytes(change_dir: Path, policy_digest: str) -> bytes:
    path = change_dir / policy_snapshot_relpath(policy_digest)
    if not path.exists():
        raise PolicyError(f"policy snapshot missing at {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read policy snapshot at {path}: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != policy_digest:
        raise PolicyError(
            f"policy snapshot digest mismatch for {path}: expected {policy_digest}, got {actual}"
        )
    return data


def _read_pinned_profile_bytes(change_dir: Path, profile_digest: str) -> bytes:
    path = change_dir / assurance_profile_snapshot_relpath(profile_digest)
    if not path.exists():
        raise PolicyError(f"assurance profile snapshot missing at {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read assurance profile snapshot at {path}: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != profile_digest:
        raise PolicyError(
            f"assurance profile snapshot digest mismatch for {path}: expected {profile_digest}, got {actual}"
        )
    parse_assurance_profile_snapshot(data)
    return data
