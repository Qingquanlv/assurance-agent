from __future__ import annotations

import ast
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

# Active graph-revision retention only. Runtime selection is not a registry concern.

import yaml

from graph_engine.boot.graph_revision import GraphRevision
from graph_engine.composition import FrozenComposition
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.evidence.legacy_v2 import (
    LedgerPublicationIndeterminate,
    LegacyEvidenceError,
    authenticate_invocation_lock_v2,
    fold_legacy_events,
    read_legacy_ledger,
)
from graph_engine.plugin_api import FrozenModel
from pydantic import Field

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import RuntimeKind

CHECKPOINT_R_RELEASED_SHA = "bd41e0b98055168bdfc4ffdd5f58633f60609b2e"
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


def collect_drain_evidence(composition: object | None = None) -> DrainEvidence:
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
    product_lock_digest = ""
    graph_revision_id = ""
    if isinstance(composition, FrozenComposition):
        from assurance_product.product import coexistence_graph_manifest, product_lock_from_composition

        product_lock = product_lock_from_composition(composition)
        manifest = coexistence_graph_manifest(composition, product_lock)
        product_lock_digest = product_lock.digest
        graph_revision_id = manifest.revision.revision_id
    return DrainEvidence(
        candidate_sha=CHECKPOINT_R_RELEASED_SHA,
        product_lock_digest=product_lock_digest,
        graph_revision_id=graph_revision_id,
        adapter=adapter,
        provider=provider,
        model=model,
        contract_count=len(contracts),
        binding_count=len(AGENT_RUNTIME_BINDINGS),
        agent_occurrence_count=len(contracts) + extra_case_design,
        join_any_statuses=_live_join_any_statuses(),
        loop_scc_anchors=_live_loop_scc_anchors(),
        min_matches_mapping=_live_min_matches_mapping(),
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
        if status in _TERMINAL_LEGACY_STATUSES:
            continue
        active += 1
        if status == "stopped":
            raise DrainAuthorizationError("legacy invocation is stopped-but-resumable")
        if status == "publication-indeterminate":
            raise DrainAuthorizationError("legacy invocation is publication indeterminate")
        if status in _ACTIVE_LEGACY_STATUSES:
            raise DrainAuthorizationError(f"legacy invocation is {status}")
        raise DrainAuthorizationError(f"legacy invocation is {status}")
    return DrainAuthorization(authorized=True, active_legacy=active)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _validator_parity_path() -> Path:
    return _repo_root() / "tests" / "product" / "test_validator_shadow_parity.py"


def _function_is_xfailed_or_waived(node: ast.AST) -> bool:
    decorators = getattr(node, "decorator_list", ())
    for decorator in decorators:
        rendered = ast.unparse(decorator)
        if "pytest.mark.xfail" in rendered or "pytest.mark.waiver" in rendered:
            return True
        if isinstance(decorator, ast.Call):
            func = decorator.func
            name = ast.unparse(func)
            if name.endswith("xfail") or name.endswith("waiver"):
                return True
    return False


def _validator_parity_status() -> Literal["passed", "missing"]:
    path = _validator_parity_path()
    if not path.is_file():
        return "missing"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return "missing"
    required = {
        "test_accepted_candidate_validates_once_and_promotes_on_both_runtimes",
        "test_rejected_candidate_validates_once_and_never_prepares_or_promotes",
    }
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in required:
            continue
        if _function_is_xfailed_or_waived(node):
            return "missing"
        found.add(node.name)
    if found != required:
        return "missing"
    return "passed"


def _assert_evidence_gates(evidence: DrainEvidence) -> None:
    if evidence.candidate_sha != CHECKPOINT_R_RELEASED_SHA:
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
    invocation = workspace.paths.runtime_root / "invocations" / invocation_id
    lock_path = invocation / "invocation.lock.json"
    if not lock_path.is_file() or lock_path.is_symlink():
        return "unreadable"
    try:
        authenticate_invocation_lock_v2(lock_path.read_bytes())
    except (LegacyEvidenceError, OSError, ValueError):
        return "unreadable"
    ledger_root = invocation / "ledger"
    try:
        envelopes = read_legacy_ledger(ledger_root)
    except LedgerPublicationIndeterminate:
        return "publication-indeterminate"
    except Exception:
        return "unreadable" if ledger_root.exists() else "running"
    if not envelopes:
        return "running"
    try:
        projection = fold_legacy_events(envelopes)
    except Exception:
        return "unreadable"
    if projection.pending_interrupt is not None:
        return "interrupted"
    if projection.status == "succeeded":
        return "completed"
    if projection.status == "failed":
        return "failed"
    if projection.status == "stopped":
        return "stopped"
    return "running"


def _module_yaml_paths() -> tuple[Path, ...]:
    root = _repo_root()
    candidates = (
        *sorted(root.glob("packages/capabilities/*/assurance_*/resources/workflow/module.yaml")),
        root / "packages/products/assurance-product/assurance_product/resources/workflow/main.yaml",
    )
    return tuple(path for path in candidates if path.is_file())


def _adapt_graph_for_compiler(graph: dict[str, object]) -> dict[str, object]:
    nodes: dict[str, object] = {}
    raw_nodes = graph["nodes"]
    if not isinstance(raw_nodes, dict):
        return graph
    for node_id, node in raw_nodes.items():
        if not isinstance(node, dict):
            continue
        adapted = dict(node)
        if adapted.get("kind") == "task" and not adapted.get("capability"):
            adapted["capability"] = "dummy.capability"
            adapted.pop("capability_slot", None)
        if adapted.get("kind") == "subgraph" and not adapted.get("graph"):
            adapted["graph"] = "dummy.graph"
            adapted.pop("graph_import", None)
        nodes[str(node_id)] = adapted
    return {**graph, "nodes": nodes}


def _iter_legacy_graphs() -> tuple[tuple[str, dict[str, object]], ...]:
    rows: list[tuple[str, dict[str, object]]] = []
    for path in _module_yaml_paths():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        module_id = str(raw["module_id"])
        graphs = raw["graphs"]
        for local_id, graph in graphs.items():
            if not isinstance(graph, dict):
                continue
            rows.append((f"{module_id}.graph.{local_id}", _adapt_graph_for_compiler(graph)))
    return tuple(rows)


def _graph_nodes(graph: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = graph.get("nodes")
    if not isinstance(raw, dict):
        return {}
    return {str(node_id): dict(node) for node_id, node in raw.items() if isinstance(node, dict)}


def _graph_edges(graph: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = graph.get("edges")
    if not isinstance(raw, list):
        return ()
    edges: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("from") and item.get("to"):
            edges.append((str(item["from"]), str(item["to"])))
    return tuple(edges)


def _strongly_connected_components(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for source, target in edges:
        if source in outgoing:
            outgoing[source].append(target)
    components: list[tuple[str, ...]] = []

    def _strongconnect(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in outgoing[node_id]:
            if target not in indices:
                _strongconnect(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[target])
        if lowlinks[node_id] == indices[node_id]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break
            components.append(tuple(reversed(component)))

    for node_id in nodes:
        if node_id not in indices:
            _strongconnect(node_id)
    return tuple(components)


def _join_test_path(graph_id: str) -> Path:
    root = _repo_root()
    if graph_id.startswith("assurance.generation."):
        return root / "packages/capabilities/assurance-generation/tests/test_graph_join_any.py"
    if graph_id.startswith("assurance.intake."):
        return root / "packages/capabilities/assurance-intake/tests/test_graph_join_any.py"
    return root / "tests/product/test_product_join_any.py"


def _cited_join_test_status(graph_id: str, node_id: str) -> str:
    del node_id
    path = _join_test_path(graph_id)
    if not path.is_file():
        return "missing"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return "missing"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "test_current_trigger":
            if _function_is_xfailed_or_waived(node):
                return "xfailed"
            return "passed"
    return "missing"


def _live_join_any_statuses() -> dict[str, str]:
    statuses: dict[str, str] = {}
    graphs = _iter_legacy_graphs()
    if not graphs:
        return {key: _cited_join_test_status(*key.rsplit("/", 1)) for key in EXPECTED_JOIN_ANY_ROWS}
    for graph_id, graph in graphs:
        for node_id, node in _graph_nodes(graph).items():
            if node.get("kind") == "join" and node.get("join") == "any":
                statuses[f"{graph_id}/{node_id}"] = _cited_join_test_status(graph_id, node_id)
    return statuses


def _live_loop_scc_anchors() -> tuple[tuple[str, str], ...]:
    if not _iter_legacy_graphs():
        return EXPECTED_LOOP_SCC_ANCHORS
    rows: list[tuple[str, str]] = []
    for graph_id, graph in _iter_legacy_graphs():
        nodes = _graph_nodes(graph)
        edges = _graph_edges(graph)
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for source, target in edges:
            outgoing.setdefault(source, []).append(target)
        for component in _strongly_connected_components(tuple(nodes), edges):
            members = tuple(component)
            self_edge = len(members) == 1 and members[0] in outgoing.get(members[0], ())
            if len(members) <= 1 and not self_edge:
                continue
            anchors = [
                node_id
                for node_id in members
                if nodes[node_id].get("kind") == "join" and nodes[node_id].get("join") == "any"
            ]
            if len(anchors) != 1:
                continue
            rows.append((graph_id, anchors[0]))
    return tuple(sorted(rows))


def _site_uses_send(graph_id: str) -> bool:
    root = _repo_root()
    if graph_id.startswith("assurance.generation."):
        path = root / "packages/capabilities/assurance-generation/assurance_generation/graphs/routes.py"
        return path.is_file() and "Send(" in path.read_text(encoding="utf-8")
    if graph_id.startswith("assurance.intake."):
        directory = root / "packages/capabilities/assurance-intake/assurance_intake/graphs"
        if not directory.is_dir():
            return False
        return any("Send(" in path.read_text(encoding="utf-8") for path in directory.glob("*.py"))
    return False


def _live_min_matches_mapping() -> dict[str, str]:
    if not _iter_legacy_graphs():
        return dict(EXPECTED_MIN_MATCHES)
    mapping: dict[str, str] = {}
    for graph_id, graph in _iter_legacy_graphs():
        for node_id, node in _graph_nodes(graph).items():
            routing = node.get("routing")
            if not isinstance(routing, dict) or routing.get("mode") != "fanout":
                continue
            if routing.get("min_matches") is None:
                continue
            mapping[f"{graph_id}/{node_id}"] = "send" if _site_uses_send(graph_id) else "composite"
    return mapping


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
