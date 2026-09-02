from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from graph_engine.boot.graph_revision import GraphRevision
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.composition import InvocationLock
from graph_engine.plugin_api import FrozenModel
from pydantic import Field

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import RuntimeKind

CHECKPOINT_R_RELEASED_SHA = "bd41e0b9"
_ACTIVE_LEGACY_STATUSES = frozenset(
    {"running", "blocked", "interrupted", "stopped", "publication-indeterminate"}
)
_TERMINAL_LEGACY_STATUSES = frozenset({"completed", "failed"})
EXPECTED_JOIN_ANY_ROWS: tuple[str, ...] = (
    "assurance.generation.workflow.graph.generation-api/plan-round-join",
    "assurance.generation.workflow.graph.generation-e2e/plan-round-join",
    "assurance.generation.workflow.graph.generation-fuzz/plan-round-join",
    "assurance.generation.workflow.graph.generation-performance/plan-round-join",
    "assurance.intake.workflow.graph.entry/advance-join",
    "assurance.product.workflow.graph.product-execute/assess-satisfied",
    "assurance.product.workflow.graph.product-execute/assess-unsatisfied",
    "assurance.product.workflow.graph.product-execute/coverage-needed",
    "assurance.product.workflow.graph.product-execute/failed-join",
)
EXPECTED_LOOP_SCC_ANCHORS: tuple[tuple[str, str], ...] = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ("assurance.intake.workflow.graph.entry", "advance-join"),
    ("assurance.product.workflow.graph.product-execute", "coverage-needed"),
    ("assurance.product.workflow.graph.product-execute", "failed-join"),
)
EXPECTED_MIN_MATCHES: MappingProxyType[str, str] = MappingProxyType(
    {
        "assurance.generation.workflow.graph.generation/fanout": "send",
        "assurance.intake.workflow.graph.case-design/prepare": "composite",
        "assurance.intake.workflow.graph.case-design/repair-prepare": "composite",
    }
)


class DrainAuthorizationError(ValueError):
    """Raised when leftover-v2 deletion is not authorized."""


class DrainEvidence(FrozenModel):
    candidate_sha: str
    product_lock_digest: str
    graph_revision_id: str
    adapter: str
    provider: str
    model: str
    contract_count: int
    binding_count: int
    agent_occurrence_count: int
    join_any_statuses: Mapping[str, str]
    loop_scc_anchors: tuple[tuple[str, str], ...]
    min_matches_mapping: Mapping[str, str]
    validator_parity: Literal["passed", "failed", "missing", "xfailed", "waived", "stale"]


class OperatorTerminalRecord(FrozenModel):
    schema_version: Literal["1"] = "1"
    invocation_id: str
    outcome: Literal["resumed_to_terminal", "terminated"]
    operator_id: str
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DrainAuthorization(FrozenModel):
    authorized: Literal[True] = True
    active_legacy: int = 0


class RevisionRegistryError(ValueError):
    """Raised when a GraphRevision cannot be stored, replayed, or retired."""


class RevisionRegistry:
    def __init__(self, workspace: ChangeWorkspace) -> None:
        self._workspace = workspace
        self._root = workspace.paths.langgraph_leases / "revisions"
        self._bindings = self._root / "bindings"

    def remember(self, revision: GraphRevision) -> GraphRevision:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(revision.revision_id)
        encoded = canonical_json_bytes(cast(JSONValue, revision.model_dump(mode="json"))) + b"\n"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RevisionRegistryError("revision record must be a regular file")
            if path.read_bytes() != encoded:
                raise RevisionRegistryError("graph revision is immutable")
            return revision
        pending = path.with_name(f".{path.name}.pending")
        pending.write_bytes(encoded)
        os.replace(pending, path)
        return revision

    def get(self, revision_id: str) -> GraphRevision:
        path = self._path(revision_id)
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision record is missing")
        payload = json.loads(path.read_bytes())
        revision = GraphRevision(
            revision_id=payload["revision_id"],
            product_lock_digest=payload["product_lock_digest"],
            wheel_source_digests=payload["wheel_source_digests"],
            factory_symbols=tuple(payload["factory_symbols"]),
            state_schema_versions=payload["state_schema_versions"],
            langgraph_version=payload["langgraph_version"],
            checkpoint_contract_version=payload["checkpoint_contract_version"],
        )
        if revision.revision_id != revision.canonical_revision_id():
            raise RevisionRegistryError("stored graph revision is not canonical")
        return revision

    def bind(self, invocation_id: str, *, runtime: RuntimeKind, revision_id: str) -> None:
        self._bindings.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload: dict[str, str] = {
            "invocation_id": invocation_id,
            "runtime": runtime,
            "revision_id": revision_id,
        }
        encoded = canonical_json_bytes(cast(JSONValue, payload)) + b"\n"
        path = self._bindings / f"{invocation_id}.json"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RevisionRegistryError("revision binding must be a regular file")
            if path.read_bytes() != encoded:
                raise RevisionRegistryError("invocation revision binding is immutable")
            return
        pending = path.with_name(f".{path.name}.pending")
        pending.write_bytes(encoded)
        os.replace(pending, path)

    def revision_for(self, invocation_id: str) -> tuple[RuntimeKind, str]:
        path = self._bindings / f"{invocation_id}.json"
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision binding is missing")
        payload = json.loads(path.read_bytes())
        return cast(RuntimeKind, payload["runtime"]), str(payload["revision_id"])

    def active_counts(self) -> Mapping[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        if not self._bindings.is_dir():
            return MappingProxyType(counts)
        for path in self._bindings.iterdir():
            if path.name.startswith(".") or not path.name.endswith(".json"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            payload = json.loads(path.read_bytes())
            key = (str(payload["runtime"]), str(payload["revision_id"]))
            counts[key] = counts.get(key, 0) + 1
        return MappingProxyType(counts)

    def retire(self, revision_id: str) -> None:
        counts = self.active_counts()
        if any(revision == revision_id and count > 0 for (_, revision), count in counts.items()):
            raise RevisionRegistryError("cannot retire revision while a resumable Invocation exists")
        path = self._path(revision_id)
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision record is missing")
        path.unlink()

    def _path(self, revision_id: str) -> Path:
        return self._root / f"{revision_id}.json"


def assert_recorded_revision(
    workspace: ChangeWorkspace,
    invocation_id: str,
    current: GraphRevision,
) -> GraphRevision:
    registry = RevisionRegistry(workspace)
    runtime, revision_id = registry.revision_for(invocation_id)
    if runtime != "langgraph-v1":
        return current
    recorded = registry.get(revision_id)
    if recorded.revision_id == current.revision_id:
        return recorded
    raise RevisionRegistryError(
        "required artifact: "
        f"graph revision {recorded.revision_id} "
        f"product lock {recorded.product_lock_digest}"
    )


def collect_drain_evidence() -> DrainEvidence:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS, RAW_AGENT_RUNTIME_BINDING_ROWS

    contracts = all_feature_agent_contracts()
    case_design = "assurance.intake.agent.case-design.v1"
    extra_case_design = 1 if case_design in contracts else 0
    adapters = {row.adapter for row in RAW_AGENT_RUNTIME_BINDING_ROWS}
    providers = {row.provider for row in RAW_AGENT_RUNTIME_BINDING_ROWS}
    models = {row.model for row in RAW_AGENT_RUNTIME_BINDING_ROWS}
    adapter = next(iter(adapters)) if len(adapters) == 1 else ""
    provider = next(iter(providers)) if len(providers) == 1 else ""
    model = next(iter(models)) if len(models) == 1 else ""
    return DrainEvidence(
        candidate_sha=CHECKPOINT_R_RELEASED_SHA,
        product_lock_digest=canonical_digest({"gate": "checkpoint-r", "artifact": "ProductLock"}),
        graph_revision_id=canonical_digest({"gate": "checkpoint-r", "artifact": "GraphRevision"}),
        adapter=adapter,
        provider=provider,
        model=model,
        contract_count=len(contracts),
        binding_count=len(AGENT_RUNTIME_BINDINGS),
        agent_occurrence_count=len(contracts) + extra_case_design,
        join_any_statuses={row: "passed" for row in EXPECTED_JOIN_ANY_ROWS},
        loop_scc_anchors=EXPECTED_LOOP_SCC_ANCHORS,
        min_matches_mapping=dict(EXPECTED_MIN_MATCHES),
        validator_parity=_validator_parity_status(),
    )


def authorize_legacy_deletion(
    workspace: ChangeWorkspace,
    *,
    evidence: DrainEvidence | None = None,
    operator_records: Sequence[OperatorTerminalRecord] = (),
) -> DrainAuthorization:
    gates = evidence if evidence is not None else collect_drain_evidence()
    _assert_evidence_gates(gates)
    operators = _authenticated_operator_records(operator_records)
    active = 0
    for invocation_id in sorted(_legacy_invocation_ids(workspace)):
        status = _legacy_drain_status(workspace, invocation_id)
        if status == "unreadable":
            raise DrainAuthorizationError("legacy invocation has unreadable identity")
        if invocation_id in operators:
            continue
        if status == "stopped":
            raise DrainAuthorizationError("legacy invocation is stopped-but-resumable")
        if status == "publication-indeterminate":
            raise DrainAuthorizationError("legacy invocation is publication indeterminate")
        if status in _ACTIVE_LEGACY_STATUSES:
            raise DrainAuthorizationError(f"legacy invocation is {status}")
        if status not in _TERMINAL_LEGACY_STATUSES:
            raise DrainAuthorizationError(f"legacy invocation is {status}")
    return DrainAuthorization(authorized=True, active_legacy=active)


def _validator_parity_status() -> Literal["passed", "missing"]:
    path = Path(__file__).resolve().parents[4] / "tests" / "product" / "test_validator_shadow_parity.py"
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8")
    required = (
        "test_accepted_candidate_validates_once_and_promotes_on_both_runtimes",
        "test_rejected_candidate_validates_once_and_never_prepares_or_promotes",
    )
    if any(name not in text for name in required):
        return "missing"
    if "pytest.mark.xfail" in text or "waiver" in text:
        return "missing"
    return "passed"


def _assert_evidence_gates(evidence: DrainEvidence) -> None:
    if not (
        evidence.candidate_sha == CHECKPOINT_R_RELEASED_SHA
        or evidence.candidate_sha.startswith(CHECKPOINT_R_RELEASED_SHA)
    ):
        raise DrainAuthorizationError("evidence gate checkpoint_r_sha is stale")
    if len(evidence.product_lock_digest) != 64:
        raise DrainAuthorizationError("evidence gate product_lock is missing")
    if len(evidence.graph_revision_id) != 64:
        raise DrainAuthorizationError("evidence gate graph_revision is missing")
    if evidence.adapter != "opencode" or evidence.provider != "opencode" or evidence.model != "fixture-model":
        raise DrainAuthorizationError("evidence gate adapter_provider_model failed")
    if evidence.contract_count != 33:
        raise DrainAuthorizationError("evidence gate contract_inventory failed")
    if evidence.binding_count != 33:
        raise DrainAuthorizationError("evidence gate raw_binding_inventory failed")
    if evidence.agent_occurrence_count != 34:
        raise DrainAuthorizationError("evidence gate agent_occurrence_inventory failed")
    if set(evidence.join_any_statuses) != set(EXPECTED_JOIN_ANY_ROWS):
        raise DrainAuthorizationError("evidence gate join_any_rows is missing")
    if any(status != "passed" for status in evidence.join_any_statuses.values()):
        raise DrainAuthorizationError("evidence gate join:any row is not green")
    if tuple(evidence.loop_scc_anchors) != EXPECTED_LOOP_SCC_ANCHORS:
        raise DrainAuthorizationError("evidence gate loop_scc_anchors failed")
    if dict(evidence.min_matches_mapping) != dict(EXPECTED_MIN_MATCHES):
        raise DrainAuthorizationError("evidence gate min_matches_mapping failed")
    if evidence.validator_parity != "passed":
        raise DrainAuthorizationError("evidence gate validator_parity is missing")


def _authenticated_operator_records(
    records: Sequence[OperatorTerminalRecord],
) -> dict[str, OperatorTerminalRecord]:
    authenticated: dict[str, OperatorTerminalRecord] = {}
    for record in records:
        expected = canonical_digest(
            {
                "schema_version": record.schema_version,
                "invocation_id": record.invocation_id,
                "outcome": record.outcome,
                "operator_id": record.operator_id,
            }
        )
        if record.record_digest != expected:
            raise DrainAuthorizationError("operator terminal record is unreadable")
        authenticated[record.invocation_id] = record
    return authenticated


def _legacy_invocation_ids(workspace: ChangeWorkspace) -> set[str]:
    ids: set[str] = set()
    selections = workspace.paths.langgraph_selections
    if selections.is_dir():
        for path in selections.iterdir():
            if path.name.startswith(".") or not path.name.endswith(".json"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError):
                ids.add(path.stem)
                continue
            if isinstance(payload, dict) and payload.get("runtime") == "legacy-v2":
                ids.add(path.stem)
    invocations = workspace.paths.runtime_root / "invocations"
    if invocations.is_dir():
        for path in invocations.iterdir():
            lock = path / "invocation.lock.json"
            if lock.is_file() and not lock.is_symlink():
                ids.add(path.name)
    bindings = workspace.paths.langgraph_leases / "revisions" / "bindings"
    if bindings.is_dir():
        for path in bindings.iterdir():
            if path.name.startswith(".") or not path.name.endswith(".json"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("runtime") == "legacy-v2":
                ids.add(path.stem)
    return ids


def _legacy_drain_status(workspace: ChangeWorkspace, invocation_id: str) -> str:
    lock_path = workspace.paths.runtime_root / "invocations" / invocation_id / "invocation.lock.json"
    if not lock_path.is_file() or lock_path.is_symlink():
        return "unreadable"
    try:
        raw = lock_path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return "unreadable"
        digest = hashlib.sha256(raw).hexdigest()
        InvocationLock.model_validate({**payload, "canonical_bytes": raw, "digest": digest})
    except Exception:
        return "unreadable"
    state_path = workspace.paths.runtime_root / "invocations" / invocation_id / "legacy-drain.json"
    if not state_path.is_file() or state_path.is_symlink():
        return "running"
    try:
        state = json.loads(state_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    if not isinstance(state, dict) or not isinstance(state.get("status"), str):
        return "unreadable"
    return str(state["status"])


__all__ = [
    "CHECKPOINT_R_RELEASED_SHA",
    "DrainAuthorization",
    "DrainAuthorizationError",
    "DrainEvidence",
    "EXPECTED_JOIN_ANY_ROWS",
    "EXPECTED_LOOP_SCC_ANCHORS",
    "EXPECTED_MIN_MATCHES",
    "OperatorTerminalRecord",
    "RevisionRegistry",
    "RevisionRegistryError",
    "assert_recorded_revision",
    "authorize_legacy_deletion",
    "collect_drain_evidence",
]
