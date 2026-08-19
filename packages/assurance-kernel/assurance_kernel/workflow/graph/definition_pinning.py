"""Pin and inherit replay definition bindings for graph invocations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from assurance_kernel.artifacts.policy import (
    PolicyError,
    PolicyOrigin,
    PolicySnapshot,
    load_policy_snapshot_bytes,
)
from assurance_kernel.verification.profile_manifest import (
    assurance_profile_bytes,
    assurance_profile_digest,
    assurance_profile_snapshot_relpath,
    parse_assurance_profile_snapshot,
)
from assurance_kernel.workflow.core.progression import ProgressionTxn
from assurance_kernel.workflow.graph.compiler import (
    CompileError,
    HistoricalCompileContext,
    PinnedDefinitionRequest,
    ResolvedPinnedDefinition,
    canonical_digest,
    compile_historical_workflow,
)
from assurance_kernel.workflow.graph.historical_roles import (
    discover_historical_assurance_roles,
)
from assurance_kernel.workflow.graph.contracts import (
    ContractError,
    ExecutionContract,
    ExecutionContractCatalog,
    catalog_from_pinned_contracts,
)
from assurance_kernel.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    parse_ingest_catalog_snapshot,
    validate_catalog_runtime,
)
from assurance_kernel.workflow.graph.models import CompiledWorkflow, GraphProjection
from assurance_kernel.workflow.graph.runtime_commit_safety import (
    SEMANTICS_ID as COMMIT_SAFETY_SEMANTICS_ID,
    commit_safety_semantics_bytes,
    commit_safety_semantics_digest,
    commit_safety_semantics_object_digest,
)
from assurance_kernel.workflow.graph.schema_v2 import WorkflowSchemaV2
from assurance_kernel.workflow.graph.topology_semantics import (
    SEMANTICS_ID as TOPOLOGY_SAFETY_SEMANTICS_ID,
    topology_safety_semantics_bytes,
    topology_safety_semantics_digest,
    topology_safety_semantics_object_digest,
)
from assurance_kernel.workflow.graph.workspace import TreeStore
from assurance_kernel.workflow.orchestration.gate_semantics import (
    SEMANTICS_ID as GATE_SEMANTICS_ID,
    canonical_descriptor_bytes,
    gate_semantics_bytes,
    gate_semantics_digest,
    gate_semantics_object_digest,
)

_POLICY_LOGICAL_PATH = "project:.aa/policy.yaml"
_SCHEMA_DIR = ".graph-runtime/schemas"
_CONTRACT_DIR = ".graph-runtime/contracts"
_CATALOG_DIR = ".graph-runtime/ingest-catalogs"
_POLICY_DIR = ".graph-runtime/policies"
_GATE_SEMANTICS_DIR = ".graph-runtime/gate-semantics"
_TOPOLOGY_SEMANTICS_DIR = ".graph-runtime/topology-semantics"
_COMMIT_SAFETY_SEMANTICS_DIR = ".graph-runtime/commit-safety-semantics"

# Phase-local inventory of v6 semantic-field consumers. Task 17 closes evidence_export.
V6_SEMANTIC_FIELD_CONSUMERS: frozenset[str] = frozenset(
    {
        "root_start_serialization",
        "child_start_serialization",
        "projection",
        "child_inheritance",
        "pinned_definition_request",
        "live_compatibility",
        "replay_compatibility",
        "snapshot_definition_context",
        "validator_effect_dispatch",
        "evidence_export",
    }
)

PinnedDefinitionReason = Literal[
    "pinned_schema_missing",
    "pinned_schema_digest_mismatch",
    "pinned_schema_compile_failed",
    "pinned_ingest_catalog_missing",
    "pinned_ingest_catalog_invalid",
    "pinned_ingest_catalog_digest_mismatch",
    "pinned_contract_snapshot_missing",
    "pinned_contract_digest_mismatch",
    "pinned_contract_target_mismatch",
    "pinned_historical_roles_invalid",
]


class PinnedDefinitionError(Exception):
    def __init__(self, reason_code: PinnedDefinitionReason, message: str) -> None:
        self.reason_code = reason_code
        self.message = message
        super().__init__(f"{reason_code}: {message}")


def request_for_compiled(
    compiled: CompiledWorkflow,
    *,
    event_schema_version: int,
    gate_digest: str | None = None,
    profile_digest: str | None = None,
) -> PinnedDefinitionRequest:
    """Build a pinned definition request from a compiled workflow identity."""
    if event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 definition requests are supported")
    v6 = current_v6_semantic_identity()
    return PinnedDefinitionRequest(
        graph_digest=compiled.digest,
        ingest_catalog_digest=compiled.ingest_catalog_digest,
        contract_digests=tuple(sorted(compiled.contract_digests.items())),
        event_schema_version=event_schema_version,
        gate_semantics_digest=gate_digest if gate_digest is not None else gate_semantics_digest(),
        assurance_profile_digest=(
            profile_digest if profile_digest is not None else assurance_profile_digest()
        ),
        gate_semantics_object_id="" if v6 is None else v6.gate_object_id,
        topology_safety_semantics_object_id="" if v6 is None else v6.topology_object_id,
        topology_safety_semantics_digest="" if v6 is None else v6.topology_digest,
        commit_safety_semantics_object_id="" if v6 is None else v6.commit_object_id,
        commit_safety_semantics_digest="" if v6 is None else v6.commit_digest,
    )


@dataclass(frozen=True, slots=True)
class _V6SemanticIdentity:
    gate_object_id: str
    gate_digest: str
    gate_bytes: bytes
    topology_object_id: str
    topology_digest: str
    topology_bytes: bytes
    commit_object_id: str
    commit_digest: str
    commit_bytes: bytes


@dataclass(frozen=True, slots=True)
class InvocationDefinitionBinding:
    event_schema_version: int
    policy_digest: str
    policy_origin: PolicyOrigin
    policy_bytes: bytes
    gate_semantics_digest: str
    assurance_profile_digest: str
    assurance_profile_bytes: bytes | None
    gate_semantics_object_id: str = ""
    gate_semantics_bytes: bytes | None = None
    topology_safety_semantics_object_id: str = ""
    topology_safety_semantics_digest: str = ""
    topology_safety_semantics_bytes: bytes | None = None
    commit_safety_semantics_object_id: str = ""
    commit_safety_semantics_digest: str = ""
    commit_safety_semantics_bytes: bytes | None = None


def policy_snapshot_relpath(policy_digest: str) -> str:
    return f"{_POLICY_DIR}/{policy_digest}.json"


def gate_semantics_snapshot_relpath(object_id: str) -> str:
    return f"{_GATE_SEMANTICS_DIR}/{object_id}.json"


def topology_semantics_snapshot_relpath(object_id: str) -> str:
    return f"{_TOPOLOGY_SEMANTICS_DIR}/{object_id}.json"


def commit_safety_semantics_snapshot_relpath(object_id: str) -> str:
    return f"{_COMMIT_SAFETY_SEMANTICS_DIR}/{object_id}.json"


def is_definition_binding_replayable(projection: GraphProjection) -> bool:
    if projection.event_schema_version != 6:
        return False
    return bool(
        projection.policy_digest
        and projection.policy_origin
        and projection.gate_semantics_digest
        and projection.assurance_profile_digest
        and projection.gate_semantics_object_id
        and projection.topology_safety_semantics_object_id
        and projection.topology_safety_semantics_digest
        and projection.commit_safety_semantics_object_id
        and projection.commit_safety_semantics_digest
    )


def bind_root_definitions(
    *,
    store: TreeStore,
    root_tree_id: str,
    event_schema_version: int = 6,
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
    if event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 root bindings are supported")
    return _binding_from_snapshot(snap, event_schema_version=event_schema_version)


def inherit_child_definitions(
    *,
    parent: GraphProjection,
    change_dir: Path,
) -> InvocationDefinitionBinding:
    """Inherit exact parent definition bindings without assurance classification."""
    if parent.event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 parent bindings can be inherited")
    if not parent.policy_digest:
        raise PolicyError("parent invocation has no pinned policy digest")
    profile_bytes = _read_pinned_profile_bytes(change_dir, parent.assurance_profile_digest)
    gate_bytes = _read_pinned_semantics_bytes(
        change_dir,
        gate_semantics_snapshot_relpath(parent.gate_semantics_object_id),
        object_id=parent.gate_semantics_object_id,
        semantic_digest=parent.gate_semantics_digest,
        semantics_id=GATE_SEMANTICS_ID,
        label="gate semantics",
    )
    topology_bytes = _read_pinned_semantics_bytes(
        change_dir,
        topology_semantics_snapshot_relpath(parent.topology_safety_semantics_object_id),
        object_id=parent.topology_safety_semantics_object_id,
        semantic_digest=parent.topology_safety_semantics_digest,
        semantics_id=TOPOLOGY_SAFETY_SEMANTICS_ID,
        label="topology semantics",
    )
    commit_bytes = _read_pinned_semantics_bytes(
        change_dir,
        commit_safety_semantics_snapshot_relpath(parent.commit_safety_semantics_object_id),
        object_id=parent.commit_safety_semantics_object_id,
        semantic_digest=parent.commit_safety_semantics_digest,
        semantics_id=COMMIT_SAFETY_SEMANTICS_ID,
        label="commit-safety semantics",
    )
    binding = InvocationDefinitionBinding(
        event_schema_version=parent.event_schema_version,
        policy_digest=parent.policy_digest,
        policy_origin=_coerce_policy_origin(parent.policy_origin),
        policy_bytes=_read_pinned_policy_bytes(change_dir, parent.policy_digest),
        gate_semantics_digest=parent.gate_semantics_digest,
        assurance_profile_digest=parent.assurance_profile_digest,
        assurance_profile_bytes=profile_bytes,
        gate_semantics_object_id=parent.gate_semantics_object_id,
        gate_semantics_bytes=gate_bytes,
        topology_safety_semantics_object_id=parent.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=parent.topology_safety_semantics_digest,
        topology_safety_semantics_bytes=topology_bytes,
        commit_safety_semantics_object_id=parent.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=parent.commit_safety_semantics_digest,
        commit_safety_semantics_bytes=commit_bytes,
    )
    verify_pinned_definitions(binding, change_dir)
    return binding


def stage_pinned_definitions(
    txn: ProgressionTxn,
    compiled: CompiledWorkflow,
    binding: InvocationDefinitionBinding,
    *,
    contracts: ExecutionContractCatalog,
    ingest_catalog: IngestArtifactCatalog | None = None,
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

    catalog = ingest_catalog if ingest_catalog is not None else validate_catalog_runtime()
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
    if binding.event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 definition bindings can be staged")
    if (
        binding.gate_semantics_bytes is None
        or binding.topology_safety_semantics_bytes is None
        or binding.commit_safety_semantics_bytes is None
    ):
        raise PolicyError("v6 definition binding requires staged semantics object bytes")
    txn.write_runtime_file_once(
        gate_semantics_snapshot_relpath(binding.gate_semantics_object_id),
        binding.gate_semantics_bytes,
    )
    txn.write_runtime_file_once(
        topology_semantics_snapshot_relpath(binding.topology_safety_semantics_object_id),
        binding.topology_safety_semantics_bytes,
    )
    txn.write_runtime_file_once(
        commit_safety_semantics_snapshot_relpath(binding.commit_safety_semantics_object_id),
        binding.commit_safety_semantics_bytes,
    )


def verify_pinned_definitions(binding: InvocationDefinitionBinding, change_dir: Path) -> None:
    if not (
        binding.policy_digest
        and binding.policy_origin
        and binding.gate_semantics_digest
        and binding.assurance_profile_digest
    ):
        raise PolicyError("definition binding is incomplete for replay")
    if binding.event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 definition bindings can be verified")
    _read_pinned_policy_bytes(change_dir, binding.policy_digest)
    if binding.assurance_profile_bytes is None:
        raise PolicyError("v6 definition binding requires assurance profile snapshot bytes")
    pinned = _read_pinned_profile_bytes(change_dir, binding.assurance_profile_digest)
    if pinned != binding.assurance_profile_bytes:
        raise PolicyError("assurance profile snapshot bytes do not match binding")
    _verify_v6_binding_fields(binding)
    if (
        binding.gate_semantics_bytes is None
        or binding.topology_safety_semantics_bytes is None
        or binding.commit_safety_semantics_bytes is None
    ):
        raise PolicyError("v6 definition binding requires semantics object bytes")
    gate_pinned = _read_pinned_semantics_bytes(
        change_dir,
        gate_semantics_snapshot_relpath(binding.gate_semantics_object_id),
        object_id=binding.gate_semantics_object_id,
        semantic_digest=binding.gate_semantics_digest,
        semantics_id=GATE_SEMANTICS_ID,
        label="gate semantics",
    )
    if gate_pinned != binding.gate_semantics_bytes:
        raise PolicyError("gate semantics object bytes do not match binding")
    topology_pinned = _read_pinned_semantics_bytes(
        change_dir,
        topology_semantics_snapshot_relpath(binding.topology_safety_semantics_object_id),
        object_id=binding.topology_safety_semantics_object_id,
        semantic_digest=binding.topology_safety_semantics_digest,
        semantics_id=TOPOLOGY_SAFETY_SEMANTICS_ID,
        label="topology semantics",
    )
    if topology_pinned != binding.topology_safety_semantics_bytes:
        raise PolicyError("topology semantics object bytes do not match binding")
    commit_pinned = _read_pinned_semantics_bytes(
        change_dir,
        commit_safety_semantics_snapshot_relpath(binding.commit_safety_semantics_object_id),
        object_id=binding.commit_safety_semantics_object_id,
        semantic_digest=binding.commit_safety_semantics_digest,
        semantics_id=COMMIT_SAFETY_SEMANTICS_ID,
        label="commit-safety semantics",
    )
    if commit_pinned != binding.commit_safety_semantics_bytes:
        raise PolicyError("commit-safety semantics object bytes do not match binding")


@lru_cache(maxsize=1)
def current_v6_semantic_identity() -> _V6SemanticIdentity:
    """Process-local canonical v6 semantic identity (object ID/digest/bytes)."""
    gate_data = gate_semantics_bytes()
    topology_data = topology_safety_semantics_bytes()
    commit_data = commit_safety_semantics_bytes()
    return _V6SemanticIdentity(
        gate_object_id=gate_semantics_object_digest(),
        gate_digest=gate_semantics_digest(),
        gate_bytes=gate_data,
        topology_object_id=topology_safety_semantics_object_digest(),
        topology_digest=topology_safety_semantics_digest(),
        topology_bytes=topology_data,
        commit_object_id=commit_safety_semantics_object_digest(),
        commit_digest=commit_safety_semantics_digest(),
        commit_bytes=commit_data,
    )


def _binding_from_snapshot(snap: PolicySnapshot, *, event_schema_version: int) -> InvocationDefinitionBinding:
    if event_schema_version != 6:
        raise PolicyError("only event_schema_version 6 snapshot bindings are supported")
    profile_data = assurance_profile_bytes()
    v6 = current_v6_semantic_identity()
    return InvocationDefinitionBinding(
        event_schema_version=event_schema_version,
        policy_digest=snap.digest,
        policy_origin=snap.origin,
        policy_bytes=snap.canonical_bytes,
        gate_semantics_digest=v6.gate_digest,
        assurance_profile_digest=assurance_profile_digest(),
        assurance_profile_bytes=profile_data,
        gate_semantics_object_id=v6.gate_object_id,
        gate_semantics_bytes=v6.gate_bytes,
        topology_safety_semantics_object_id=v6.topology_object_id,
        topology_safety_semantics_digest=v6.topology_digest,
        topology_safety_semantics_bytes=v6.topology_bytes,
        commit_safety_semantics_object_id=v6.commit_object_id,
        commit_safety_semantics_digest=v6.commit_digest,
        commit_safety_semantics_bytes=v6.commit_bytes,
    )


def _verify_v6_binding_fields(binding: InvocationDefinitionBinding) -> None:
    fields = (
        binding.gate_semantics_object_id,
        binding.gate_semantics_digest,
        binding.topology_safety_semantics_object_id,
        binding.topology_safety_semantics_digest,
        binding.commit_safety_semantics_object_id,
        binding.commit_safety_semantics_digest,
    )
    if not all(fields):
        raise PolicyError("v6 definition binding requires complete semantic object ID/digest fields")


def _read_pinned_semantics_bytes(
    change_dir: Path,
    relpath: str,
    *,
    object_id: str,
    semantic_digest: str,
    semantics_id: str,
    label: str,
) -> bytes:
    path = change_dir / relpath
    if not path.exists():
        raise PolicyError(f"{label} snapshot missing at {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read {label} snapshot at {path}: {exc}") from exc
    actual_object = hashlib.sha256(data).hexdigest()
    if actual_object != object_id:
        raise PolicyError(
            f"{label} object digest mismatch for {path}: expected {object_id}, got {actual_object}"
        )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"{label} snapshot at {path} is malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise PolicyError(f"{label} snapshot at {path} must be a JSON object")
    recorded_id = payload.get("semantics_id")
    if recorded_id != semantics_id:
        raise PolicyError(f"unknown semantic ID {recorded_id!r} for {label}")
    recorded_digest = payload.get("semantic_digest")
    if recorded_digest != semantic_digest:
        raise PolicyError(
            f"{label} semantic digest mismatch for {path}: expected {semantic_digest}, got {recorded_digest}"
        )
    try:
        recomputed = canonical_descriptor_bytes(
            semantics_id=str(payload["semantics_id"]),
            schema_version=str(payload["schema_version"]),
            runtime_versions={str(k): str(v) for k, v in dict(payload["runtime_versions"]).items()},
            dependencies=[dict(item) for item in payload["dependencies"]],
            consumers=tuple(str(item) for item in payload["consumers"]),
            semantic_digest=str(payload["semantic_digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError(f"{label} snapshot at {path} is invalid: {exc}") from exc
    if recomputed != data:
        raise PolicyError(f"{label} snapshot bytes are not canonical for {path}")
    return data


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


def _canonical_contract_snapshot_bytes(contract: ExecutionContract) -> bytes:
    return (
        json.dumps(
            contract.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _load_pinned_schema(change_dir: Path, digest: str) -> WorkflowSchemaV2:
    path = change_dir / _SCHEMA_DIR / f"{digest}.json"
    if not path.exists():
        raise PinnedDefinitionError(
            "pinned_schema_missing",
            f"pinned schema missing at {path}",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PinnedDefinitionError(
            "pinned_schema_digest_mismatch",
            f"cannot read pinned schema at {path}: {exc}",
        ) from exc
    try:
        schema = WorkflowSchemaV2.model_validate(json.loads(data))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PinnedDefinitionError(
            "pinned_schema_digest_mismatch",
            f"pinned schema at {path} is invalid: {exc}",
        ) from exc
    return schema


def _load_pinned_ingest_catalog(change_dir: Path, digest: str) -> IngestArtifactCatalog:
    path = change_dir / _CATALOG_DIR / f"{digest}.json"
    if not path.exists():
        raise PinnedDefinitionError(
            "pinned_ingest_catalog_missing",
            f"pinned ingest catalog missing at {path}",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PinnedDefinitionError(
            "pinned_ingest_catalog_invalid",
            f"cannot read pinned ingest catalog at {path}: {exc}",
        ) from exc
    try:
        catalog = parse_ingest_catalog_snapshot(data)
    except ValueError as exc:
        raise PinnedDefinitionError("pinned_ingest_catalog_invalid", str(exc)) from exc
    if catalog.digest != digest:
        raise PinnedDefinitionError(
            "pinned_ingest_catalog_digest_mismatch",
            f"pinned ingest catalog digest mismatch for {path}: expected {digest}, got {catalog.digest}",
        )
    return catalog


def _load_pinned_execution_contracts(
    change_dir: Path,
    recorded: Mapping[str, str],
) -> ExecutionContractCatalog:
    loaded: list[ExecutionContract] = []
    for target, digest in sorted(recorded.items()):
        path = change_dir / _CONTRACT_DIR / f"{digest}.json"
        if not path.exists():
            raise PinnedDefinitionError(
                "pinned_contract_snapshot_missing",
                f"pinned contract snapshot missing for {target!r} at {path}",
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"cannot read pinned contract snapshot at {path}: {exc}",
            ) from exc
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"pinned contract snapshot at {path} is malformed: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"pinned contract snapshot at {path} must be a JSON object",
            )
        try:
            contract = ExecutionContract.model_validate(payload)
        except ValidationError as exc:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"pinned contract snapshot at {path} is invalid: {exc}",
            ) from exc
        if _canonical_contract_snapshot_bytes(contract) != data:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"pinned contract snapshot bytes are not canonical for {path}",
            )
        if contract.target != target:
            raise PinnedDefinitionError(
                "pinned_contract_target_mismatch",
                f"pinned contract target mismatch for {path}: recorded {target!r}, snapshot {contract.target!r}",
            )
        actual_digest = canonical_digest(contract)
        if actual_digest != digest:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"pinned contract digest mismatch for {path}: expected {digest}, got {actual_digest}",
            )
        loaded.append(contract)
    try:
        return catalog_from_pinned_contracts(loaded)
    except ContractError as exc:
        raise PinnedDefinitionError(
            "pinned_contract_digest_mismatch",
            str(exc),
        ) from exc


def _pinned_reason_for_compile_error(exc: CompileError) -> PinnedDefinitionReason:
    for diagnostic in exc.diagnostics:
        if diagnostic.category == "historical_ingest_identity":
            return "pinned_ingest_catalog_digest_mismatch"
        if diagnostic.category == "historical_contract_identity":
            return "pinned_contract_digest_mismatch"
        if diagnostic.category == "workflow_validation":
            return "pinned_schema_compile_failed"
    # Compatibility fallback for callers that construct bare string CompileError.
    message = str(exc)
    if "ingest_catalog_digest" in message:
        return "pinned_ingest_catalog_digest_mismatch"
    if "contract target set" in message or ("contract" in message and "digest expected" in message):
        return "pinned_contract_digest_mismatch"
    return "pinned_schema_compile_failed"


def load_pinned_execution_definition(
    change_dir: Path,
    request: PinnedDefinitionRequest,
) -> ResolvedPinnedDefinition:
    recorded_contracts = dict(request.contract_digests)
    schema = _load_pinned_schema(change_dir, request.graph_digest)
    ingest = _load_pinned_ingest_catalog(change_dir, request.ingest_catalog_digest)
    contracts = _load_pinned_execution_contracts(change_dir, recorded_contracts)
    unrecorded = sorted(
        {
            node.uses
            for graph in schema.graphs.values()
            for node in graph.nodes.values()
            if not node.uses.startswith("graph:") and node.uses not in recorded_contracts
        }
    )
    if unrecorded:
        raise PinnedDefinitionError(
            "pinned_contract_digest_mismatch",
            f"unrecorded referenced contract targets: {unrecorded}",
        )
    historical_roles, role_issues = discover_historical_assurance_roles(schema)
    if historical_roles is None:
        detail = "; ".join(f"{issue.code}:{issue.detail}" for issue in role_issues) or "discovery failed"
        raise PinnedDefinitionError("pinned_historical_roles_invalid", detail)
    # Ambiguous/incomplete role discovery fails closed at the load boundary.
    blocking = [
        issue
        for issue in role_issues
        if issue.code
        in {
            "missing_unique_role",
            "duplicate_role",
            "ambiguous_alias",
            "disconnected_lineage",
            "ambiguous_layer_binding",
        }
    ]
    if blocking:
        detail = "; ".join(f"{issue.code}:{issue.detail}" for issue in blocking)
        raise PinnedDefinitionError("pinned_historical_roles_invalid", detail)
    try:
        compiled = compile_historical_workflow(
            schema,
            context=HistoricalCompileContext(
                ingest_catalog=ingest,
                ingest_catalog_digest=request.ingest_catalog_digest,
                contracts=contracts,
                contract_digests=recorded_contracts,
                historical_roles=historical_roles,
            ),
        )
    except CompileError as exc:
        raise PinnedDefinitionError(
            _pinned_reason_for_compile_error(exc),
            str(exc),
        ) from exc
    if compiled.digest != request.graph_digest:
        raise PinnedDefinitionError(
            "pinned_schema_digest_mismatch",
            f"pinned schema digest mismatch: expected {request.graph_digest}, compiled {compiled.digest}",
        )
    if compiled.ingest_catalog_digest != request.ingest_catalog_digest:
        raise PinnedDefinitionError(
            "pinned_ingest_catalog_digest_mismatch",
            "compiled ingest catalog digest does not match recorded root digest",
        )
    if compiled.contract_digests != recorded_contracts:
        recorded_targets = set(recorded_contracts)
        compiled_targets = set(compiled.contract_digests)
        missing = sorted(compiled_targets - recorded_targets)
        extra = sorted(recorded_targets - compiled_targets)
        if missing:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"unrecorded referenced contract targets: {missing}",
            )
        if extra:
            raise PinnedDefinitionError(
                "pinned_contract_digest_mismatch",
                f"extra conflicting contract bindings: {extra}",
            )
        raise PinnedDefinitionError(
            "pinned_contract_digest_mismatch",
            "compiled contract digests do not match recorded root contract_digests",
        )
    return ResolvedPinnedDefinition(
        compiled=compiled,
        contracts=contracts,
        ingest_catalog=ingest,
        historical_roles=historical_roles,
    )
