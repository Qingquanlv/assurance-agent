"""Dark-ship current-chain / selected-test codegen scoring (activated in Task 22)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.artifacts.models.generated_files import GeneratedFilesV1
from assurance_agent.eval import write_scan
from assurance_agent.eval.evidence_export import ExecutionEvidenceV1
from assurance_agent.eval.scorers import shared
from assurance_agent.verification.generated_entries import (
    GeneratedEntryDecision,
    LayerMappingRelation,
    MappedTestEntry,
    classify_generated_entry,
    extract_layer_mapping,
)
from assurance_agent.verification.generated_files import (
    get_generated_files_contract,
    get_generated_files_model,
)
from assurance_agent.workflow.graph.precommit import (
    GENERATED_FILES_CANDIDATE_V1,
    CandidateValidationReceiptV1,
)
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotV1
from assurance_agent.workflow.graph.workspace import WriteEntry, WriteSet

HARD_METRIC_NAMES = (
    "current_assurance_chain_rate",
    "current_codegen_attempt_rate",
    "selected_test_write_rate",
)

ChainRole = Literal[
    "applicability",
    "preflight",
    "reviewer",
    "plan_gate",
    "precheck",
    "codegen",
]


@dataclass(frozen=True, slots=True)
class AttemptBinding:
    """One epoch-closed attempt identity for a chain role."""

    role: ChainRole
    attempt_id: str
    task_id: str
    generation_id: str
    source_tree_id: str
    target_tree_id: str
    output_digests: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WriteAttribution:
    """Evidence needed to credit one selected private-root test write."""

    write_set: WriteSet
    receipt: CandidateValidationReceiptV1
    input_snapshot: TaskInputSnapshotV1
    input_snapshot_id: str
    runtime_context_sha256: str | None
    manifest: GeneratedFilesV1
    summary_digest: str
    manifest_digest: str
    content_changed_paths: frozenset[str]
    source_by_path: Mapping[str, str]
    mapping: LayerMappingRelation
    private_root: str
    selected_sibling_roots: frozenset[str] = frozenset()
    forbidden_write_count: int = 0


@dataclass(frozen=True, slots=True)
class CurrentLayerBinding:
    """Typed current-chain package for one selected layer (test seam + binder input)."""

    layer: LayerName
    applicable: bool
    root_invocation_id: str
    assurance_invocation_id: str
    branch_invocation_id: str
    cycle_generation_id: str
    attempts: tuple[AttemptBinding, ...]
    na_document_ok: bool = False
    plan_attempt_present: bool = False
    write_attribution: WriteAttribution | None = None
    policy_integrity_ok: bool = True


@dataclass(frozen=True, slots=True)
class CurrentLayerCodegenEvidence:
    layer: LayerName
    applicable: bool
    chain_ok: bool
    codegen_attempt_ok: bool
    selected_test_write_ok: bool
    policy_integrity_ok: bool
    reason_code: str = "ok"
    entry_decisions: tuple[GeneratedEntryDecision, ...] = ()


def calculate_current_assurance_chain_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    if not evidences:
        return 0.0
    return sum(1.0 for item in evidences if item.chain_ok) / len(evidences)


def calculate_current_codegen_attempt_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    applicable = [item for item in evidences if item.applicable]
    if not applicable:
        return 0.0
    return sum(1.0 for item in applicable if item.codegen_attempt_ok) / len(applicable)


def calculate_selected_test_write_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    applicable = [item for item in evidences if item.applicable]
    if not applicable:
        return 0.0
    return sum(1.0 for item in applicable if item.selected_test_write_ok) / len(applicable)


def evaluate_layer_binding(binding: CurrentLayerBinding) -> CurrentLayerCodegenEvidence:
    """Evaluate one selected-layer current-chain package into typed evidence."""
    if not binding.policy_integrity_ok:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=binding.applicable,
            chain_ok=False,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=False,
            reason_code="policy_integrity_failed",
        )

    chain_ok, chain_reason = _evaluate_chain(binding)
    if not chain_ok:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=binding.applicable,
            chain_ok=False,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=True,
            reason_code=chain_reason,
        )

    if not binding.applicable:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=False,
            chain_ok=True,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=True,
            reason_code="inapplicable_ok",
        )

    codegen_ok = _codegen_attempt_ok(binding)
    write_ok, decisions, write_reason = _selected_test_write_ok(binding)
    return CurrentLayerCodegenEvidence(
        layer=binding.layer,
        applicable=True,
        chain_ok=True,
        codegen_attempt_ok=codegen_ok,
        selected_test_write_ok=write_ok and codegen_ok,
        policy_integrity_ok=True,
        reason_code="ok"
        if write_ok and codegen_ok
        else write_reason
        if codegen_ok
        else "codegen_attempt_missing",
        entry_decisions=decisions,
    )


def score_current_codegen_metrics(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> dict[str, float]:
    """Callable hard-metric calculations (not registered on the live scorer)."""
    return {
        "current_assurance_chain_rate": calculate_current_assurance_chain_rate(evidences),
        "current_codegen_attempt_rate": calculate_current_codegen_attempt_rate(evidences),
        "selected_test_write_rate": calculate_selected_test_write_rate(evidences),
    }


def replay_policy_integrity(attempt_dir: Path) -> bool:
    """Require byte-identical reconstructed WritePolicyV1 before write credit."""
    return shared.replay_policy_integrity(attempt_dir)


def bind_and_score_attempt(attempt_dir: Path) -> dict[str, float]:
    """Bind exported root-slice evidence into hard metrics (zero on integrity failure)."""
    zeros = {name: 0.0 for name in HARD_METRIC_NAMES}
    if not shared.replay_policy_integrity(attempt_dir):
        return zeros
    try:
        bindings = bind_layer_packages(attempt_dir)
    except Exception:
        return zeros
    if bindings is None:
        return zeros
    evidences = [evaluate_layer_binding(item) for item in bindings]
    return score_current_codegen_metrics(evidences)


def bind_layer_packages(attempt_dir: Path) -> list[CurrentLayerBinding] | None:
    """Construct one typed current-chain package per selected layer from export evidence."""
    envelope = load_execution_envelope(attempt_dir)
    if envelope is None or envelope.root_invocation_id is None:
        return None
    if envelope.root_slice is None or envelope.export_manifest is None:
        return None
    export_root = _resolve_evidence_path(attempt_dir, envelope.root_slice.relative_path)
    manifest_path = _resolve_evidence_path(attempt_dir, envelope.export_manifest.relative_path)
    if export_root is None or manifest_path is None:
        return None
    # root_slice.relative_path points at the slice file; objects live beside it.
    export_dir = export_root.parent
    try:
        from assurance_agent.eval.evidence_export import (
            EvidenceExportManifestV1,
            RootEventSliceV1,
            verify_export_manifest_closure,
        )

        slice_model = RootEventSliceV1.model_validate_json(export_root.read_bytes())
        manifest = EvidenceExportManifestV1.model_validate_json(manifest_path.read_bytes())
        verify_export_manifest_closure(manifest=manifest, export_dir=export_dir)
    except Exception:
        return None
    if slice_model.root_invocation_id != envelope.root_invocation_id:
        return None

    objects = _index_export_objects(export_dir, manifest)
    roles = _load_packaged_roles()
    if roles is None:
        return None

    events = [item.event.model_dump(mode="json") for item in slice_model.events]
    bindings: list[CurrentLayerBinding] = []
    for layer in envelope.selected_layers:
        layer_roles = _layer_roles(roles, layer)
        if layer_roles is None:
            return None
        binding = _bind_one_layer(
            layer=layer,
            layer_roles=layer_roles,
            root_invocation_id=envelope.root_invocation_id,
            events=events,
            objects=objects,
            attempt_dir=attempt_dir,
            envelope_selected=tuple(envelope.selected_layers),
        )
        bindings.append(binding)
    return bindings


def load_plan_case_bytes_from_snapshot(
    *,
    input_snapshot: TaskInputSnapshotV1,
    input_snapshot_id: str,
    blobs: Mapping[str, bytes],
    plan_logical: str,
    case_logicals: Sequence[str],
) -> tuple[str, list[dict[str, object]]]:
    """Load plan/case bytes only from attempt-bound snapshot/exported blobs.

    Callers must pass the snapshot already bound by receipt ``input_snapshot_id``.
    ``runtime_context_sha256`` is retained on the snapshot for D13 consumers.
    """
    if not input_snapshot_id:
        raise ValueError("input_snapshot_id is required")
    _ = input_snapshot.runtime_context_sha256
    _ = input_snapshot.input_sha256
    plan_text = _blob_text_for_logical(input_snapshot, blobs, plan_logical)
    cases: list[dict[str, object]] = []
    for logical in case_logicals:
        raw = _blob_bytes_for_logical(input_snapshot, blobs, logical)
        text = raw.decode("utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = yaml.safe_load(text)
        if not isinstance(payload, dict):
            raise ValueError(f"case blob for {logical} must be an object")
        cases.append(payload)
    return plan_text, cases


def classify_write_set_entries(
    *,
    layer: str,
    attribution: WriteAttribution,
) -> tuple[bool, tuple[GeneratedEntryDecision, ...]]:
    """Credit only current mapped behavioral selected private-root test writes."""
    private = attribution.private_root.rstrip("/")
    prefix = private + "/"
    decisions: list[GeneratedEntryDecision] = []
    credited = False

    # Zero sibling / forbidden policy writes.
    for entry in attribution.write_set.entries:
        path = _logical_to_repo(entry.logical_path)
        if any(
            path == root or path.startswith(root.rstrip("/") + "/")
            for root in attribution.selected_sibling_roots
        ):
            return False, ()
    if attribution.forbidden_write_count:
        return False, ()

    # Receipt must be generated_files_candidate/v1 and bind digests.
    if attribution.receipt.validator_id != GENERATED_FILES_CANDIDATE_V1:
        return False, ()
    if attribution.receipt.input_snapshot_id != attribution.input_snapshot_id:
        return False, ()
    if attribution.receipt.write_set_id != attribution.write_set.write_set_id:
        return False, ()
    if not attribution.write_set.entries:
        return False, ()

    contract = get_generated_files_contract(layer)
    summary_key = contract.summary_path.removeprefix("change:")
    manifest_key = contract.manifest_path.removeprefix("change:")
    outputs = {
        str(key).removeprefix("change:"): _normalize_digest(value)
        for key, value in dict(attribution.receipt.output_digests).items()
    }
    if outputs.get(summary_key) != _normalize_digest(attribution.summary_digest):
        return False, ()
    if outputs.get(manifest_key) != _normalize_digest(attribution.manifest_digest):
        return False, ()

    mapped_targets = {entry.target_file: entry for entry in attribution.mapping.entries}
    selected = set(attribution.mapping.selected_case_ids)
    role_by_path = {item.repo_path: item for item in attribution.manifest.files}

    for write in attribution.write_set.entries:
        path = _logical_to_repo(write.logical_path)
        if not (path == private or path.startswith(prefix)):
            continue
        if write.operation not in {"add", "modify"}:
            continue
        after = _normalize_digest(write.after_sha256)
        blob = _normalize_digest(write.blob_sha256)
        before = _normalize_digest(write.before_sha256)
        if after is None or blob is None or after == before:
            continue
        if after != blob:
            continue
        manifest_entry = role_by_path.get(path)
        if manifest_entry is None or manifest_entry.role != "test_entry":
            continue
        if _normalize_digest(manifest_entry.content_sha256) != after:
            continue
        if path not in attribution.content_changed_paths:
            continue
        mapped = mapped_targets.get(path)
        if mapped is None or mapped.case_id not in selected:
            continue
        source = attribution.source_by_path.get(path)
        if source is None:
            continue
        decision = classify_generated_entry(layer=layer, source=source, entry=mapped)
        decisions.append(decision)
        if decision.accepted:
            credited = True

    return credited, tuple(decisions)


def _normalize_digest(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("sha256:")


def _evaluate_chain(binding: CurrentLayerBinding) -> tuple[bool, str]:
    by_role = {item.role: item for item in binding.attempts}
    if binding.applicable:
        required: tuple[ChainRole, ...] = (
            "applicability",
            "reviewer",
            "plan_gate",
            "precheck",
            "codegen",
        )
        if binding.layer in {"fuzz", "performance"}:
            required = ("applicability", "preflight", *required[1:])
        for role in required:
            if role not in by_role:
                return False, f"missing_{role}"
        if binding.plan_attempt_present:
            return False, "plan_attempt_in_codegen_only"
        generation = binding.cycle_generation_id
        trees: set[tuple[str, str]] = set()
        for role in required:
            attempt = by_role[role]
            if attempt.generation_id != generation:
                return False, "epoch_generation_mismatch"
            trees.add((attempt.source_tree_id, attempt.target_tree_id))
        # All roles must share one contiguous producer tree chain identity.
        if len({by_role[role].generation_id for role in required}) != 1:
            return False, "epoch_generation_mismatch"
        return True, "ok"

    # Inapplicable: current N/A + required absence of reviewer/codegen.
    if not binding.na_document_ok:
        return False, "missing_na_document"
    if "applicability" not in by_role:
        return False, "missing_applicability"
    if binding.layer in {"fuzz", "performance"} and "preflight" not in by_role:
        return False, "missing_preflight"
    if "reviewer" in by_role or "codegen" in by_role:
        return False, "unexpected_reviewer_or_codegen"
    return True, "ok"


def _codegen_attempt_ok(binding: CurrentLayerBinding) -> bool:
    codegen = next((item for item in binding.attempts if item.role == "codegen"), None)
    if codegen is None:
        return False
    contract = get_generated_files_contract(binding.layer)
    summary_key = contract.summary_path.removeprefix("change:")
    manifest_key = contract.manifest_path.removeprefix("change:")
    digests = dict(codegen.output_digests)
    summary = digests.get(summary_key)
    manifest = digests.get(manifest_key)
    if binding.write_attribution is not None:
        return (
            summary == binding.write_attribution.summary_digest
            and manifest == binding.write_attribution.manifest_digest
        )
    return bool(summary and manifest)


def _selected_test_write_ok(
    binding: CurrentLayerBinding,
) -> tuple[bool, tuple[GeneratedEntryDecision, ...], str]:
    attribution = binding.write_attribution
    if attribution is None:
        return False, (), "missing_write_attribution"
    ok, decisions = classify_write_set_entries(layer=binding.layer, attribution=attribution)
    if not ok:
        return False, decisions, "selected_test_write_failed"
    return True, decisions, "ok"


def _logical_to_repo(logical_path: str) -> str:
    for prefix in ("repo:", "project:", "change:"):
        if logical_path.startswith(prefix):
            return logical_path.removeprefix(prefix)
    return logical_path


def _blob_bytes_for_logical(
    snapshot: TaskInputSnapshotV1,
    blobs: Mapping[str, bytes],
    logical: str,
) -> bytes:
    for entry in snapshot.entries:
        if logical in entry.logical_aliases or entry.repo_relpath == logical.removeprefix("change:"):
            if entry.sha256 is None:
                raise ValueError(f"snapshot entry for {logical} has no sha256")
            digest = entry.sha256.removeprefix("sha256:")
            raw = blobs.get(entry.sha256) or blobs.get(digest)
            if raw is None:
                raise ValueError(f"missing blob for {logical}")
            return raw
    raise ValueError(f"logical {logical} not present in input snapshot")


def _blob_text_for_logical(
    snapshot: TaskInputSnapshotV1,
    blobs: Mapping[str, bytes],
    logical: str,
) -> str:
    return _blob_bytes_for_logical(snapshot, blobs, logical).decode("utf-8")


def make_zero_evidence(layer: LayerName, *, applicable: bool) -> CurrentLayerCodegenEvidence:
    return CurrentLayerCodegenEvidence(
        layer=layer,
        applicable=applicable,
        chain_ok=False,
        codegen_attempt_ok=False,
        selected_test_write_ok=False,
        policy_integrity_ok=False,
        reason_code="zero",
    )


def with_policy_failure(evidence: CurrentLayerCodegenEvidence) -> CurrentLayerCodegenEvidence:
    return replace(
        evidence,
        chain_ok=False,
        codegen_attempt_ok=False,
        selected_test_write_ok=False,
        policy_integrity_ok=False,
        reason_code="policy_integrity_failed",
    )


def mapping_from_snapshot_bytes(
    *,
    layer: str,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
) -> LayerMappingRelation:
    return extract_layer_mapping(layer=layer, plan_text=plan_text, cases=cases)


def digest_bytes(data: bytes) -> str:
    return sha256_bytes(data)


def digest_model(model: object) -> str:
    return sha256_bytes(canonical_json_bytes(model))


def load_execution_envelope(attempt_dir: Path) -> ExecutionEvidenceV1 | None:
    path = attempt_dir / "execution.json"
    if not path.is_file():
        return None
    try:
        return ExecutionEvidenceV1.model_validate_json(path.read_bytes())
    except Exception:
        return None


def _resolve_evidence_path(attempt_dir: Path, relative_path: str) -> Path | None:
    direct = attempt_dir / relative_path
    if direct.is_file():
        return direct
    under_evidence = attempt_dir / "evidence" / relative_path
    if under_evidence.is_file():
        return under_evidence
    nested = attempt_dir / "evidence" / Path(relative_path).name
    if nested.is_file():
        return nested
    return None


def _index_export_objects(export_dir: Path, manifest: object) -> dict[tuple[str, str], Path]:
    out: dict[tuple[str, str], Path] = {}
    objects = getattr(manifest, "objects", ())
    for obj in objects:
        out[(obj.kind, obj.logical_id)] = export_dir / obj.relative_path
    return out


def _load_packaged_roles():
    from assurance_agent.workflow.graph.historical_roles import discover_historical_assurance_roles
    from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin

    loaded = load_workflow_v2_with_origin(Path("/"))
    roles, _issues = discover_historical_assurance_roles(loaded.schema)
    return roles


def _layer_roles(roles: object, layer: LayerName):
    from assurance_agent.workflow.graph.historical_roles import layer_roles_or_none

    return layer_roles_or_none(roles, layer)  # type: ignore[arg-type]


def _bind_one_layer(
    *,
    layer: LayerName,
    layer_roles: object,
    root_invocation_id: str,
    events: Sequence[Mapping[str, object]],
    objects: Mapping[tuple[str, str], Path],
    attempt_dir: Path,
    envelope_selected: tuple[str, ...],
) -> CurrentLayerBinding:
    assurance_id = _child_invocation(events, parent=root_invocation_id, graph_id="assurance")
    branch_id = _child_invocation(
        events,
        parent=assurance_id or "",
        graph_id=getattr(layer_roles, "branch_graph_id", ""),
    )
    cycle_id = _child_invocation(
        events,
        parent=branch_id or "",
        graph_id=getattr(layer_roles, "cycle_graph_id", ""),
    )
    if not assurance_id or not branch_id or not cycle_id:
        return CurrentLayerBinding(
            layer=layer,
            applicable=False,
            root_invocation_id=root_invocation_id,
            assurance_invocation_id=assurance_id or "",
            branch_invocation_id=branch_id or "",
            cycle_generation_id=cycle_id or "",
            attempts=(),
            na_document_ok=False,
            policy_integrity_ok=True,
        )

    started = _started_by_task(events)
    succeeded: dict[str, Mapping[str, object]] = {
        task_id: event
        for event in events
        if event.get("type") == "task_attempt_succeeded"
        and isinstance((task_id := event.get("task_id")), str)
    }
    role_node = {
        "applicability": getattr(layer_roles, "applicability_node_id", ""),
        "reviewer": getattr(layer_roles, "reviewer_node_id", ""),
        "plan_gate": getattr(layer_roles, "plan_gate_node_id", ""),
        "precheck": getattr(layer_roles, "precondition_node_id", ""),
        "codegen": getattr(layer_roles, "codegen_node_id", ""),
        "preflight": "applicability-preflight",
    }
    attempts: list[AttemptBinding] = []
    applicability_value: Mapping[str, object] | None = None
    for role, node_id in role_node.items():
        if not node_id:
            continue
        if role == "preflight" and layer not in {"fuzz", "performance"}:
            continue
        inv_scope = branch_id if role in {"preflight", "precheck", "codegen"} else cycle_id
        match = _latest_succeeded_for_node(
            started=started,
            succeeded=succeeded,
            node_id=node_id,
            invocation_id=inv_scope,
        )
        if match is None:
            continue
        start_event, success_event = match
        if role == "applicability":
            value = success_event.get("value")
            if isinstance(value, Mapping):
                applicability_value = value
        trees = _trees_for_superstep(events, str(success_event.get("superstep_id") or ""))
        outputs_raw = success_event.get("outputs_sha256")
        outputs_map = outputs_raw if isinstance(outputs_raw, Mapping) else {}
        attempts.append(
            AttemptBinding(
                role=role,  # type: ignore[arg-type]
                attempt_id=str(success_event.get("attempt_id") or ""),
                task_id=str(success_event.get("task_id") or ""),
                generation_id=cycle_id,
                source_tree_id=trees[0],
                target_tree_id=trees[1],
                output_digests={str(k).removeprefix("change:"): str(v) for k, v in outputs_map.items()},
            )
        )

    applicable = bool(applicability_value and applicability_value.get("applicable") is True)
    na_ok = bool(applicability_value and applicability_value.get("applicable") is False)
    plan_present = _plan_attempt_present(started, succeeded, cycle_id)
    write_attribution = None
    if applicable:
        write_attribution = _build_write_attribution(
            layer=layer,
            attempts=attempts,
            succeeded=succeeded,
            objects=objects,
            attempt_dir=attempt_dir,
            selected_layers=envelope_selected,
        )
    return CurrentLayerBinding(
        layer=layer,
        applicable=applicable,
        root_invocation_id=root_invocation_id,
        assurance_invocation_id=assurance_id,
        branch_invocation_id=branch_id,
        cycle_generation_id=cycle_id,
        attempts=tuple(attempts),
        na_document_ok=na_ok,
        plan_attempt_present=plan_present,
        write_attribution=write_attribution,
        policy_integrity_ok=True,
    )


def _child_invocation(
    events: Sequence[Mapping[str, object]],
    *,
    parent: str,
    graph_id: str,
) -> str | None:
    if not parent or not graph_id:
        return None
    for event in events:
        if event.get("type") != "graph_invocation_started":
            continue
        if event.get("parent_invocation_id") == parent and event.get("graph_id") == graph_id:
            inv = event.get("invocation_id")
            if isinstance(inv, str):
                return inv
    return None


def _started_by_task(
    events: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    out: dict[str, Mapping[str, object]] = {}
    for event in events:
        if event.get("type") != "task_attempt_started":
            continue
        task_id = event.get("task_id")
        if isinstance(task_id, str):
            out[task_id] = event
    return out


def _latest_succeeded_for_node(
    *,
    started: Mapping[str, Mapping[str, object]],
    succeeded: Mapping[str, Mapping[str, object]],
    node_id: str,
    invocation_id: str,
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    matches: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for task_id, start in started.items():
        if start.get("node_id") != node_id:
            continue
        if start.get("invocation_id") != invocation_id:
            continue
        success = succeeded.get(task_id)
        if success is None:
            continue
        matches.append((start, success))
    return matches[-1] if matches else None


def _trees_for_superstep(
    events: Sequence[Mapping[str, object]],
    superstep_id: str,
) -> tuple[str, str]:
    if not superstep_id:
        return "", ""
    for event in events:
        if event.get("type") != "superstep_committed":
            continue
        if event.get("superstep_id") != superstep_id:
            continue
        return str(event.get("base_tree_id") or ""), str(event.get("target_tree_id") or "")
    return "", ""


def _plan_attempt_present(
    started: Mapping[str, Mapping[str, object]],
    succeeded: Mapping[str, Mapping[str, object]],
    cycle_id: str,
) -> bool:
    """True when a planner task succeeded in this cycle (forbidden under codegen-only)."""
    # Planner node id is exactly "plan". Do not match mechanical-plan-checks / review-gate.
    for task_id, start in started.items():
        if start.get("invocation_id") != cycle_id:
            continue
        if start.get("node_id") == "plan" and task_id in succeeded:
            return True
    return False


def _build_write_attribution(
    *,
    layer: LayerName,
    attempts: Sequence[AttemptBinding],
    succeeded: Mapping[str, Mapping[str, object]],
    objects: Mapping[tuple[str, str], Path],
    attempt_dir: Path,
    selected_layers: Sequence[str],
) -> WriteAttribution | None:
    codegen = next((item for item in attempts if item.role == "codegen"), None)
    if codegen is None:
        return None
    success = succeeded.get(codegen.task_id)
    if success is None:
        return None
    write_set_id = success.get("write_set_id")
    receipt_id = success.get("candidate_validation_receipt_id")
    snapshot_id = success.get("input_snapshot_id")
    if (
        not isinstance(write_set_id, str)
        or not isinstance(receipt_id, str)
        or not isinstance(snapshot_id, str)
    ):
        return None
    write_set_path = objects.get(("write_set", write_set_id))
    receipt_path = objects.get(("validation_receipt", receipt_id))
    snapshot_path = objects.get(("input_snapshot", snapshot_id))
    if write_set_path is None or receipt_path is None or snapshot_path is None:
        return None
    try:
        write_set = WriteSet.model_validate_json(write_set_path.read_bytes())
        receipt = CandidateValidationReceiptV1.model_validate_json(receipt_path.read_bytes())
        snapshot = TaskInputSnapshotV1.model_validate_json(snapshot_path.read_bytes())
    except Exception:
        return None

    contract = get_generated_files_contract(layer)
    summary_key = contract.summary_path.removeprefix("change:")
    manifest_key = contract.manifest_path.removeprefix("change:")
    summary_digest = codegen.output_digests.get(summary_key, "")
    manifest_digest = codegen.output_digests.get(manifest_key, "")
    if not summary_digest or not manifest_digest:
        return None

    # Manifest bytes may be on the write set as a change: blob.
    manifest_model = _load_manifest_model(
        layer, write_set=write_set, objects=objects, manifest_key=manifest_key
    )
    if manifest_model is None:
        return None

    blobs = _load_export_blobs(objects)
    try:
        plan_logical = next(
            (
                alias
                for entry in snapshot.entries
                for alias in entry.logical_aliases
                if alias.startswith("change:")
                and ("codegen-plan" in alias or alias.endswith(f"/{layer}-plan.md"))
            ),
            f"change:plans/{layer}-codegen-plan.md",
        )
        # Prefer change: aliases once per case file (YAML/JSON).
        case_logicals = sorted(
            {
                alias
                for entry in snapshot.entries
                for alias in entry.logical_aliases
                if alias.startswith("change:") and alias.endswith("case.yaml")
            }
        )
        plan_text, cases = load_plan_case_bytes_from_snapshot(
            input_snapshot=snapshot,
            input_snapshot_id=snapshot_id,
            blobs=blobs,
            plan_logical=plan_logical,
            case_logicals=case_logicals,
        )
        mapping = extract_layer_mapping(layer=layer, plan_text=plan_text, cases=cases)
    except Exception:
        return None

    source_by_path: dict[str, str] = {}
    for entry in write_set.entries:
        path = _logical_to_repo(entry.logical_path)
        if entry.blob_sha256 is None:
            continue
        raw = blobs.get(entry.blob_sha256) or blobs.get(entry.blob_sha256.removeprefix("sha256:"))
        if raw is None:
            continue
        try:
            source_by_path[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

    content_changed = _content_changed_paths(attempt_dir)
    sibling_roots = frozenset(
        get_generated_files_contract(other).private_test_root for other in selected_layers if other != layer
    )
    forbidden = int(shared.score_forbidden_write_executed_count(attempt_dir))
    return WriteAttribution(
        write_set=write_set,
        receipt=receipt,
        input_snapshot=snapshot,
        input_snapshot_id=snapshot_id,
        runtime_context_sha256=snapshot.runtime_context_sha256,
        manifest=manifest_model,
        summary_digest=summary_digest,
        manifest_digest=manifest_digest,
        content_changed_paths=content_changed,
        source_by_path=source_by_path,
        mapping=mapping,
        private_root=contract.private_test_root,
        selected_sibling_roots=sibling_roots,
        forbidden_write_count=forbidden,
    )


def _load_manifest_model(
    layer: LayerName,
    *,
    write_set: WriteSet,
    objects: Mapping[tuple[str, str], Path],
    manifest_key: str,
):
    logical = f"change:{manifest_key}"
    for entry in write_set.entries:
        if entry.logical_path in {logical, manifest_key} and entry.blob_sha256:
            digest = entry.blob_sha256
            path = objects.get(("blob", digest)) or objects.get(("blob", digest.removeprefix("sha256:")))
            if path is None:
                # Some exports store blob under bare digest filename.
                for key, candidate in objects.items():
                    if key[0] == "blob" and digest.endswith(key[1]):
                        path = candidate
                        break
            if path is not None and path.is_file():
                try:
                    return get_generated_files_model(layer).model_validate_json(path.read_bytes())
                except Exception:
                    return None
    return None


def _load_export_blobs(objects: Mapping[tuple[str, str], Path]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for (kind, logical_id), path in objects.items():
        if kind != "blob" or not path.is_file():
            continue
        data = path.read_bytes()
        blobs[logical_id] = data
        blobs[f"sha256:{logical_id}"] = data
        digest = sha256_bytes(data)
        blobs[digest] = data
        blobs[digest.removeprefix("sha256:")] = data
    return blobs


def _content_changed_paths(attempt_dir: Path) -> frozenset[str]:
    try:
        before = write_scan.load_worktree_manifest(attempt_dir, write_scan.WRITE_MANIFEST_BEFORE)
        after = write_scan.load_worktree_manifest(attempt_dir, write_scan.WRITE_MANIFEST_AFTER)
        persisted = write_scan.load_write_diff(attempt_dir)
        if before is None or after is None or persisted is None:
            return frozenset()
        write_scan.replay_write_diff(before=before, after=after, persisted=persisted)
        changed: set[str] = set()
        for entry in persisted.entries:
            path = str(entry.path or "")
            if path.startswith("tests/"):
                changed.add(path)
                continue
            idx = path.find("/tests/")
            if idx >= 0:
                changed.add(path[idx + 1 :])
        return frozenset(changed)
    except Exception:
        return frozenset()


__all__ = [
    "HARD_METRIC_NAMES",
    "AttemptBinding",
    "CurrentLayerBinding",
    "CurrentLayerCodegenEvidence",
    "WriteAttribution",
    "WriteEntry",
    "WriteSet",
    "bind_and_score_attempt",
    "calculate_current_assurance_chain_rate",
    "calculate_current_codegen_attempt_rate",
    "calculate_selected_test_write_rate",
    "classify_write_set_entries",
    "digest_bytes",
    "digest_model",
    "evaluate_layer_binding",
    "get_generated_files_model",
    "load_execution_envelope",
    "load_plan_case_bytes_from_snapshot",
    "make_zero_evidence",
    "mapping_from_snapshot_bytes",
    "MappedTestEntry",
    "replay_policy_integrity",
    "score_current_codegen_metrics",
    "with_policy_failure",
    "write_scan",
]
