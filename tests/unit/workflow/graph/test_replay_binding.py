"""Strict root/definition/commit/tree binding for assurance replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
import yaml

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.artifacts.policy import load_policy, normalized_policy_bytes
from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profile_manifest import assurance_profile_digest
from assurance_agent.workflow.core.graph_events import (
    GraphInvocationStartedEvent,
    GraphTerminalEvent,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.graph.compiler import (
    CompileError,
    PinnedDefinitionRequest,
    canonical_digest,
    compile_workflow,
)
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.definition_pinning import (
    PinnedDefinitionError,
    _pinned_reason_for_compile_error,
    bind_root_definitions,
    load_pinned_execution_definition,
    policy_snapshot_relpath,
)
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.replay_binding import (
    ReplayBindingError,
    bind_replay_definitions,
    normalize_logical_path,
    recover_layer_inputs,
)
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest
from assurance_agent.workflow.orchestration.gates import (
    FrozenGateReport,
    GateEvaluationContext,
    check_gate_in_view,
)
from tests.helpers_aa import write_aa_config

_CHANGE_ID = "CH-REPLAY-BIND-001"
_ENTRYPOINT = "full"
_ROOT_INV = "inv-root-full"
_PARAMS = {
    "run_mode": "full",
    "test_types": ["api", "e2e"],
    "run_tests": False,
    "auto_archive": False,
    "force_continue": False,
    "max_plan_fix_attempts": 3,
}


def _write_object(change_dir: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = change_dir / ".graph-runtime" / "objects" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


def _write_tree(change_dir: Path, *, project: Path, artifacts: dict[str, bytes]) -> str:
    entries: list[dict[str, object]] = []
    for logical, data in sorted(artifacts.items()):
        root, _, rel = logical.partition(":")
        prefix = {
            "change": f"qa/changes/{_CHANGE_ID}",
            "project": ".",
            "repo": ".",
        }[root]
        tree_path = rel if prefix == "." else f"{prefix}/{rel}"
        entries.append(
            {
                "executable": False,
                "kind": "file",
                "path": tree_path,
                "sha256": _write_object(change_dir, data),
            }
        )
    raw = json.dumps(
        {
            "entries": entries,
            "kind": "tree",
            "roots": {"change": f"qa/changes/{_CHANGE_ID}", "project": ".", "repo": "."},
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _write_object(change_dir, raw)


def _append_event(change_dir: Path, seq: int, payload: dict[str, object]) -> None:
    line = json.dumps({"seq": seq, "ts": f"2026-07-31T00:{seq:02d}:00Z", **payload}, sort_keys=True)
    path = change_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _applicable_checks(layer: str) -> PlanCheckDocument:
    profile_layer = layer
    case_id = f"TC_{layer.upper()}_001"
    return run_plan_checks(
        CheckContext(
            plan_texts={path: "# plan\n" for path in get_layer_assurance_profile(layer).plan_artifacts},
            cases=(
                {
                    "added": [
                        {
                            "case_id": case_id,
                            "title": "x",
                            "type": profile_layer.upper(),
                            "automation": {"required": True},
                            "assertions": ["ok"],
                        }
                    ],
                    "modified": [],
                },
            ),
            data_knowledge={"version": 1, "capabilities": {"domain_factories": {}}},
            layer=layer,  # type: ignore[arg-type]
        )
    )


def _inapplicable_checks(layer: LayerName) -> PlanCheckDocument:
    return run_plan_checks(
        CheckContext(
            plan_texts={},
            cases=(),
            data_knowledge={"version": 1, "capabilities": {"domain_factories": {}}},
            layer=layer,
        )  # type: ignore[arg-type]
    )


def get_layer_assurance_profile(layer: str):
    from assurance_agent.verification.profiles import get_layer_assurance_profile as _get

    return _get(layer)  # type: ignore[arg-type]


def _review(layer: str) -> PlanReview:
    return PlanReview.model_validate(
        {
            "schema_version": "1.0",
            "decision": "pass",
            "review_type": f"{layer}-plan",
            "change_id": _CHANGE_ID,
            "codegen_readiness": "ready",
            "required_capabilities": ["auth.api_admin_token"],
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
        }
    )


def _data_knowledge() -> DataKnowledge:
    return DataKnowledge.model_validate(
        {
            "version": 1,
            "auth": {"api_admin_token": {"method": "token", "symbol": "API_ADMIN_TOKEN"}},
            "capabilities": {
                "domain_factories": {},
                "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
                "cleanup": {},
            },
        }
    )


def _gate_report(
    *,
    profile,
    review: PlanReview | None,
    checks: PlanCheckDocument,
    data_knowledge: DataKnowledge | None,
    params: dict[str, object],
    project: Path,
    change_dir: Path,
) -> dict[str, object]:
    review_payload = review.model_dump(mode="json") if review is not None else {}
    checks_payload = checks.model_dump(mode="json")
    dk_payload = data_knowledge.model_dump(mode="json") if data_knowledge is not None else {}
    review_bytes = (
        (json.dumps(review_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if review is not None
        else b""
    )
    checks_bytes = (json.dumps(checks_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    dk_bytes = (
        yaml.safe_dump(dk_payload, sort_keys=False, allow_unicode=True).encode("utf-8")
        if data_knowledge is not None
        else b""
    )
    context = GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        change_id=_CHANGE_ID,
        params=params,
        state_values={},
        node_results={},
        artifact_overrides={
            profile.review_artifact: review_payload,
            profile.checks_artifact: checks_payload,
            "repo:.aa/data-knowledge.yaml": dk_payload,
        },
        audit_events_dir=project / "empty-audit",
    )
    schema = load_workflow_v2(Path.cwd())
    report = check_gate_in_view(schema.gates, profile.gate_id, context)
    reads_sha256 = dict(report.reads_sha256)
    if review is not None:
        reads_sha256[profile.review_artifact] = hashlib.sha256(review_bytes).hexdigest()
    reads_sha256[profile.checks_artifact] = hashlib.sha256(checks_bytes).hexdigest()
    if data_knowledge is not None:
        reads_sha256["repo:.aa/data-knowledge.yaml"] = hashlib.sha256(dk_bytes).hexdigest()
    return {
        "gate_id": report.gate_id,
        "verdict": report.verdict.value,
        "matched_rule": report.matched_rule,
        "reason": report.reason,
        "reads_sha256": reads_sha256,
        "value": report.verdict.value,
        "details": dict(report.details) if report.details else None,
    }


@dataclass
class ReplayBindingFixture:
    project: Path
    change_dir: Path
    compiled: object
    contracts: object
    binding: object
    root_tree: str
    assurance_inv: str
    api_cycle_inv: str
    e2e_cycle_inv: str
    seq: int = 0
    gate_trees: dict[str, str] = field(default_factory=dict)
    events_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.events_path = self.change_dir / "events.jsonl"
        self.events_path.write_text("", encoding="utf-8")

    def append(self, payload: dict[str, object]) -> None:
        self.seq += 1
        _append_event(self.change_dir, self.seq, payload)

    def _started(
        self,
        *,
        invocation_id: str,
        entrypoint: str,
        graph_id: str,
        structural_path: str,
        checkpoint_ns: str,
        params: dict[str, object],
        parent_invocation_id: str | None = None,
        parent_task_id: str | None = None,
        root_tree_id: str | None = None,
    ) -> GraphInvocationStartedEvent:
        compiled = self.compiled
        return GraphInvocationStartedEvent(
            type="graph_invocation_started",
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            graph_id=graph_id,
            graph_digest=compiled.digest,  # type: ignore[attr-defined]
            event_schema_version=4,
            ir_digest=compiled.digest,  # type: ignore[attr-defined]
            ingest_catalog_digest=compiled.ingest_catalog_digest or "cat-digest",  # type: ignore[attr-defined]
            contract_digests=dict(compiled.contract_digests),  # type: ignore[attr-defined]
            policy_digest=self.binding.policy_digest,  # type: ignore[attr-defined]
            policy_origin=self.binding.policy_origin,  # type: ignore[attr-defined]
            gate_semantics_digest=self.binding.gate_semantics_digest,  # type: ignore[attr-defined]
            assurance_profile_digest=self.binding.assurance_profile_digest,  # type: ignore[attr-defined]
            params=params,
            params_sha256=canonical_digest(params),
            root_tree_id=root_tree_id or self.root_tree,
            max_parallel_tasks=2,
            checkpoint_ns=checkpoint_ns,
            parent_invocation_id=parent_invocation_id,
            parent_task_id=parent_task_id,
            structural_path=structural_path,
        )

    def _commit_cycle(
        self,
        *,
        invocation_id: str,
        checkpoint_ns: str,
        structural_path: str,
        layer: str,
        applicable: bool,
        include_decoy_gate: bool = False,
    ) -> str:
        profile = get_layer_assurance_profile(layer)
        params = dict(_PARAMS)
        review = _review(layer) if applicable else None
        checks = _applicable_checks(layer) if applicable else _inapplicable_checks(cast(LayerName, layer))
        dk = _data_knowledge() if applicable else None

        review_bytes = (
            (json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode("utf-8")
            if review is not None
            else b""
        )
        checks_bytes = (json.dumps(checks.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        dk_bytes = (
            yaml.safe_dump(dk.model_dump(mode="json"), sort_keys=False, allow_unicode=True).encode("utf-8")
            if dk is not None
            else b""
        )
        artifacts = {normalize_logical_path(f"change:{profile.checks_artifact}"): checks_bytes}
        if applicable:
            artifacts[normalize_logical_path(f"change:{profile.review_artifact}")] = review_bytes
            artifacts[normalize_logical_path("repo:.aa/data-knowledge.yaml")] = dk_bytes
        gate_tree = _write_tree(self.change_dir, project=self.project, artifacts=artifacts)
        self.gate_trees[layer] = gate_tree

        if include_decoy_gate:
            self._task_success(
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                structural_path=structural_path,
                node_id=_GATE_NODE,
                tree_id=gate_tree,
                contract_digest="decoy-contract",
                gate_report={
                    "gate_id": profile.gate_id,
                    "verdict": "reject",
                    "matched_rule": "reject_when",
                    "reason": "decoy",
                    "reads_sha256": {},
                },
            )

        mech_task = f"{structural_path}:mechanical-plan-checks"
        mech_attempt = f"{mech_task}-a1"
        mech_digest = hashlib.sha256(checks_bytes).hexdigest()
        mech_contract = self.compiled.contract_digests["operation:verify-plan-mechanical"]  # type: ignore[attr-defined]
        gate_contract = self.compiled.contract_digests["builtin:gate"]  # type: ignore[attr-defined]
        self.append(
            SuperstepPlannedEvent(
                type="superstep_planned",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-mech",
                checkpoint_id=f"{invocation_id}-cp-mech",
                task_ids=[mech_task],
            ).model_dump(mode="json")
        )
        self.append(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-mech",
                task_id=mech_task,
                attempt_id=mech_attempt,
                node_id="mechanical-plan-checks",
                input_sha256="in-mech",
                graph_digest=self.compiled.digest,  # type: ignore[attr-defined]
                contract_digest=mech_contract,
                attempt_number=1,
                lease_expires_at="2026-07-31T00:00:00Z",
                started_at="2026-07-31T00:00:00Z",
            ).model_dump(mode="json")
        )
        self.append(
            TaskAttemptSucceededEvent(
                type="task_attempt_succeeded",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-mech",
                task_id=mech_task,
                attempt_id=mech_attempt,
                write_set_id="ws-mech",
                outputs_sha256={normalize_logical_path(f"change:{profile.checks_artifact}"): mech_digest},
            ).model_dump(mode="json")
        )
        self.append(
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-mech",
                checkpoint_id=f"{invocation_id}-cp-mech-commit",
                write_set_ids=["ws-mech"],
                target_tree_id=gate_tree,
                state_values={},
                committed_task_ids=[mech_task],
            ).model_dump(mode="json")
        )

        gate_report = _gate_report(
            profile=profile,
            review=review,
            checks=checks,
            data_knowledge=dk,
            params=params,
            project=self.project,
            change_dir=self.change_dir,
        )
        gate_task = f"{structural_path}:review-gate"
        gate_attempt = f"{gate_task}-a1"
        self.append(
            SuperstepPlannedEvent(
                type="superstep_planned",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-gate",
                checkpoint_id=f"{invocation_id}-cp-gate",
                task_ids=[gate_task],
            ).model_dump(mode="json")
        )
        self.append(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-gate",
                task_id=gate_task,
                attempt_id=gate_attempt,
                node_id="review-gate",
                input_sha256="in-gate",
                graph_digest=self.compiled.digest,  # type: ignore[attr-defined]
                contract_digest=gate_contract,
                attempt_number=1,
                lease_expires_at="2026-07-31T00:00:00Z",
                started_at="2026-07-31T00:00:00Z",
            ).model_dump(mode="json")
        )
        self.append(
            TaskAttemptSucceededEvent(
                type="task_attempt_succeeded",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-gate",
                task_id=gate_task,
                attempt_id=gate_attempt,
                gate_report=gate_report,
            ).model_dump(mode="json")
        )
        self.append(
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=f"{invocation_id}-ss-gate",
                checkpoint_id=f"{invocation_id}-cp-gate-commit",
                write_set_ids=[],
                target_tree_id=gate_tree,
                state_values={},
                committed_task_ids=[gate_task],
            ).model_dump(mode="json")
        )
        self.append(
            GraphTerminalEvent(
                type="graph_completed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                reason="done",
            ).model_dump(mode="json")
        )
        return gate_tree

    def _task_success(self, **kwargs: object) -> None:
        invocation_id = str(kwargs["invocation_id"])
        checkpoint_ns = str(kwargs["checkpoint_ns"])
        structural_path = str(kwargs["structural_path"])
        node_id = str(kwargs["node_id"])
        tree_id = str(kwargs["tree_id"])
        contract_digest = str(kwargs["contract_digest"])
        gate_report = kwargs.get("gate_report")
        task_id = f"{structural_path}:{node_id}"
        attempt_id = f"{task_id}-a1"
        ss = f"{invocation_id}-ss-{node_id}"
        self.append(
            SuperstepPlannedEvent(
                type="superstep_planned",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                checkpoint_id=f"{invocation_id}-cp-{node_id}",
                task_ids=[task_id],
            ).model_dump(mode="json")
        )
        self.append(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                task_id=task_id,
                attempt_id=attempt_id,
                node_id=node_id,
                input_sha256="in",
                graph_digest=self.compiled.digest,  # type: ignore[attr-defined]
                contract_digest=contract_digest,
                attempt_number=1,
                lease_expires_at="2026-07-31T00:00:00Z",
                started_at="2026-07-31T00:00:00Z",
            ).model_dump(mode="json")
        )
        success_payload: dict[str, object] = {
            "type": "task_attempt_succeeded",
            "invocation_id": invocation_id,
            "checkpoint_ns": checkpoint_ns,
            "superstep_id": ss,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }
        if gate_report is not None:
            success_payload["gate_report"] = gate_report
        self.append(success_payload)
        self.append(
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                checkpoint_id=f"{invocation_id}-cp-{node_id}-commit",
                write_set_ids=[],
                target_tree_id=tree_id,
                state_values={},
                committed_task_ids=[task_id],
            ).model_dump(mode="json")
        )


_GATE_NODE = "review-gate"


def _stage_pinned_definition_snapshots(
    change_dir: Path,
    *,
    compiled: object,
    schema: object,
    contracts: object,
) -> None:
    schema_bytes = (
        json.dumps(
            schema.model_dump(mode="json", by_alias=True, exclude_none=True),  # type: ignore[attr-defined]
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    schema_dir = change_dir / ".graph-runtime" / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / f"{compiled.digest}.json").write_bytes(schema_bytes)  # type: ignore[attr-defined]

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
    catalog_dir = change_dir / ".graph-runtime" / "ingest-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / f"{catalog.digest}.json").write_bytes(catalog_bytes)

    contract_dir = change_dir / ".graph-runtime" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    for target, digest in sorted(compiled.contract_digests.items()):  # type: ignore[attr-defined]
        contract = contracts.contracts[target]  # type: ignore[attr-defined]
        payload = (
            json.dumps(
                contract.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        (contract_dir / f"{digest}.json").write_bytes(payload)


def _build_fixture(
    tmp_path: Path, *, include_e2e: bool = True, api_applicable: bool = True
) -> ReplayBindingFixture:
    project = tmp_path / "project"
    change_dir = project / "qa" / "changes" / _CHANGE_ID
    change_dir.mkdir(parents=True)
    write_aa_config(project)
    policy = load_policy(project)
    policy_path = project / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    if not policy_path.exists():
        policy_path.write_bytes(normalized_policy_bytes(policy))

    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_workflow(schema, contracts)
    _stage_pinned_definition_snapshots(
        change_dir,
        compiled=compiled,
        schema=schema,
        contracts=contracts,
    )

    store = TreeStore(change_dir)
    root_tree = store.capture(project)
    binding = bind_root_definitions(store=store, root_tree_id=root_tree)
    (change_dir / policy_snapshot_relpath(binding.policy_digest)).parent.mkdir(parents=True, exist_ok=True)
    (change_dir / policy_snapshot_relpath(binding.policy_digest)).write_bytes(binding.policy_bytes)

    fixture = ReplayBindingFixture(
        project=project,
        change_dir=change_dir,
        compiled=compiled,
        contracts=contracts,
        binding=binding,
        root_tree=root_tree,
        assurance_inv=derive_child_invocation_id_from("main:assurance", "assurance"),
        api_cycle_inv=derive_child_invocation_id_from(
            "main/assurance/assurance/api/api-branch/review-cycle", "api-plan-cycle"
        ),
        e2e_cycle_inv=derive_child_invocation_id_from(
            "main/assurance/assurance/e2e/e2e-branch/review-cycle", "e2e-plan-cycle"
        ),
    )

    root_started = fixture._started(
        invocation_id=_ROOT_INV,
        entrypoint=_ENTRYPOINT,
        graph_id="main",
        structural_path="main",
        checkpoint_ns=_ROOT_INV,
        params=dict(_PARAMS),
        root_tree_id=root_tree,
    )
    fixture.append(root_started.model_dump(mode="json"))

    assurance_path = "main/assurance/assurance"
    assurance_started = fixture._started(
        invocation_id=fixture.assurance_inv,
        entrypoint="assurance",
        graph_id="assurance",
        structural_path=assurance_path,
        checkpoint_ns=f"{_ROOT_INV}/assurance/{fixture.assurance_inv}",
        params=dict(_PARAMS),
        parent_invocation_id=_ROOT_INV,
        parent_task_id="main:assurance",
    )
    fixture.append(assurance_started.model_dump(mode="json"))
    fixture.append(
        GraphTerminalEvent(
            type="graph_completed",
            invocation_id=fixture.assurance_inv,
            checkpoint_ns=assurance_started.checkpoint_ns,
            reason="assurance-done",
        ).model_dump(mode="json")
    )

    api_branch_path = f"{assurance_path}/api/api-branch"
    api_branch_inv = derive_child_invocation_id_from(f"{assurance_path}:api", "api-branch")
    api_branch_started = fixture._started(
        invocation_id=api_branch_inv,
        entrypoint="api-branch",
        graph_id="api-branch",
        structural_path=api_branch_path,
        checkpoint_ns=f"{assurance_started.checkpoint_ns}/api/{api_branch_inv}",
        params=dict(_PARAMS),
        parent_invocation_id=fixture.assurance_inv,
        parent_task_id=f"{assurance_path}:api",
    )
    fixture.append(api_branch_started.model_dump(mode="json"))
    api_cycle_path = f"{api_branch_path}/review-cycle/api-plan-cycle"
    api_cycle_started = fixture._started(
        invocation_id=fixture.api_cycle_inv,
        entrypoint="api-plan-cycle",
        graph_id="api-plan-cycle",
        structural_path=api_cycle_path,
        checkpoint_ns=f"{api_branch_started.checkpoint_ns}/review-cycle/{fixture.api_cycle_inv}",
        params=dict(_PARAMS),
        parent_invocation_id=api_branch_inv,
        parent_task_id=f"{api_branch_path}:review-cycle",
    )
    fixture.append(api_cycle_started.model_dump(mode="json"))
    fixture._commit_cycle(
        invocation_id=fixture.api_cycle_inv,
        checkpoint_ns=api_cycle_started.checkpoint_ns,
        structural_path=api_cycle_path,
        layer="api",
        applicable=api_applicable,
    )

    e2e_branch_path = f"{assurance_path}/e2e/e2e-branch"
    e2e_branch_inv = derive_child_invocation_id_from(f"{assurance_path}:e2e", "e2e-branch")
    e2e_branch_started = fixture._started(
        invocation_id=e2e_branch_inv,
        entrypoint="e2e-branch",
        graph_id="e2e-branch",
        structural_path=e2e_branch_path,
        checkpoint_ns=f"{assurance_started.checkpoint_ns}/e2e/{e2e_branch_inv}",
        params=dict(_PARAMS),
        parent_invocation_id=fixture.assurance_inv,
        parent_task_id=f"{assurance_path}:e2e",
    )
    fixture.append(e2e_branch_started.model_dump(mode="json"))
    e2e_cycle_path = f"{e2e_branch_path}/review-cycle/e2e-plan-cycle"
    e2e_cycle_started = fixture._started(
        invocation_id=fixture.e2e_cycle_inv,
        entrypoint="e2e-plan-cycle",
        graph_id="e2e-plan-cycle",
        structural_path=e2e_cycle_path,
        checkpoint_ns=f"{e2e_branch_started.checkpoint_ns}/review-cycle/{fixture.e2e_cycle_inv}",
        params=dict(_PARAMS),
        parent_invocation_id=e2e_branch_inv,
        parent_task_id=f"{e2e_branch_path}:review-cycle",
    )
    fixture.append(e2e_cycle_started.model_dump(mode="json"))
    if include_e2e:
        fixture._commit_cycle(
            invocation_id=fixture.e2e_cycle_inv,
            checkpoint_ns=e2e_cycle_started.checkpoint_ns,
            structural_path=e2e_cycle_path,
            layer="e2e",
            applicable=True,
        )

    fixture.append(
        GraphTerminalEvent(
            type="graph_completed",
            invocation_id=_ROOT_INV,
            checkpoint_ns=_ROOT_INV,
            reason="root-done",
        ).model_dump(mode="json")
    )
    return fixture


def derive_child_invocation_id_from(parent_task_id: str, graph_id: str) -> str:
    return canonical_digest({"parent_task_id": parent_task_id, "graph_id": graph_id})


def test_module_imports() -> None:
    from assurance_agent.workflow.graph import replay_binding as module

    assert callable(module.bind_replay_definitions)


def test_bind_replay_definitions_happy_path(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
        store=TreeStore(fixture.change_dir),
    )
    assert binding.root_invocation_id == _ROOT_INV
    assert binding.assurance_invocation_id == fixture.assurance_inv
    assert binding.event_schema_version == 4
    assert binding.layer_topologies["api"].status == "wired"
    assert binding.layer_topologies["e2e"].status == "wired"
    assert binding.layer_topologies["fuzz"].status == "partial"
    assert binding.layer_topologies["performance"].status == "partial"
    assert binding.gate_semantics_compatible is True
    assert binding.profile_compatibility["api"] is True
    assert "api" in binding.selected_layers
    assert "e2e" in binding.selected_layers


def test_bind_replay_definitions_missing_root(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    with pytest.raises(ReplayBindingError, match="root_invocation_unbound"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id="inv-missing",
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_wrong_entrypoint(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    with pytest.raises(ReplayBindingError, match="root_invocation_unbound"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint="archive",
        )


def test_bind_replay_definitions_later_root_not_used(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    later = fixture._started(
        invocation_id="inv-root-later",
        entrypoint=_ENTRYPOINT,
        graph_id="main",
        structural_path="main",
        checkpoint_ns="inv-root-later",
        params=dict(_PARAMS),
    )
    fixture.append(later.model_dump(mode="json"))
    fixture.append(
        GraphTerminalEvent(
            type="graph_completed",
            invocation_id="inv-root-later",
            checkpoint_ns="inv-root-later",
            reason="later",
        ).model_dump(mode="json")
    )
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.root_invocation_id == _ROOT_INV


def test_bind_replay_definitions_pinned_schema_missing(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    schema_path = fixture.change_dir / ".graph-runtime" / "schemas" / f"{fixture.compiled.digest}.json"  # type: ignore[attr-defined]
    schema_path.unlink()
    with pytest.raises(ReplayBindingError, match="pinned_schema_missing"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_schema_digest_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    schema_path = fixture.change_dir / ".graph-runtime" / "schemas" / f"{fixture.compiled.digest}.json"  # type: ignore[attr-defined]
    schema_path.write_text('{"schema_version":"2","name":"bad"}', encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="pinned_schema_digest_mismatch"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_policy_snapshot_missing(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    policy_path = fixture.change_dir / policy_snapshot_relpath(fixture.binding.policy_digest)  # type: ignore[attr-defined]
    policy_path.unlink()
    with pytest.raises(ReplayBindingError, match="policy_snapshot_missing"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_gate_semantics_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    mutated = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["gate_semantics_digest"] = "0" * 64
        mutated.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(mutated) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.gate_semantics_compatible is False
    assert binding.layer_topologies["fuzz"].status == "partial"
    with pytest.raises(ReplayBindingError, match="gate_semantics_mismatch"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_bind_replay_definitions_assurance_profile_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    mutated = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["assurance_profile_digest"] = "0" * 64
        mutated.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(mutated) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.profile_compatibility["api"] is False
    assert binding.layer_topologies["fuzz"].status == "partial"
    with pytest.raises(ReplayBindingError, match="profile_definition_incompatible"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_bind_replay_definitions_ambiguous_assurance_invocation(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    duplicate = fixture._started(
        invocation_id="inv-assurance-dup",
        entrypoint="assurance",
        graph_id="assurance",
        structural_path="main/assurance/assurance",
        checkpoint_ns="dup-ns",
        params=dict(_PARAMS),
        parent_invocation_id=_ROOT_INV,
        parent_task_id="main:assurance",
    )
    fixture.append(duplicate.model_dump(mode="json"))
    with pytest.raises(ReplayBindingError, match="ambiguous_assurance_invocation"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_wrong_assurance_parent_task(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "graph_invocation_started" and payload.get("graph_id") == "assurance":
            payload = dict(payload)
            payload["parent_task_id"] = "main:wrong"
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="ambiguous_assurance_invocation"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_recover_layer_inputs_api_applicable(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
        store=TreeStore(fixture.change_dir),
    )
    recovered = recover_layer_inputs(
        binding,
        layer="api",
        change_dir=fixture.change_dir,
        store=TreeStore(fixture.change_dir),
    )
    assert recovered.layer == "api"
    assert recovered.review is not None
    assert recovered.checks.model.layer == "api"
    assert recovered.data_knowledge is not None
    assert (
        recovered.mechanical_execution_contract_digest
        == fixture.compiled.contract_digests["operation:verify-plan-mechanical"]  # type: ignore[attr-defined]
    )
    assert isinstance(recovered.baseline, FrozenGateReport)


def test_recover_layer_inputs_inapplicable_checks_only(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False, api_applicable=False)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
        store=TreeStore(fixture.change_dir),
    )
    recovered = recover_layer_inputs(
        binding,
        layer="api",
        change_dir=fixture.change_dir,
        store=TreeStore(fixture.change_dir),
    )
    assert recovered.review is None
    assert recovered.data_knowledge is None
    assert recovered.checks.model.applicability is not None
    assert recovered.checks.model.applicability.applicable is False


def test_recover_layer_inputs_ignores_uncommitted_gate(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    api_cycle_path = "main/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle"
    gate_task = f"{api_cycle_path}:review-gate"
    fixture.append(
        TaskAttemptStartedEvent(
            type="task_attempt_started",
            invocation_id=fixture.api_cycle_inv,
            checkpoint_ns=f"ns/{fixture.api_cycle_inv}",
            superstep_id="ss-uncommitted",
            task_id=gate_task,
            attempt_id=f"{gate_task}-a-uncommitted",
            node_id="review-gate",
            input_sha256="in",
            graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
            contract_digest="gate-contract-v2",
            attempt_number=1,
            lease_expires_at="2026-07-31T00:00:00Z",
            started_at="2026-07-31T00:00:00Z",
        ).model_dump(mode="json")
    )
    fixture.append(
        TaskAttemptSucceededEvent(
            type="task_attempt_succeeded",
            invocation_id=fixture.api_cycle_inv,
            checkpoint_ns=f"ns/{fixture.api_cycle_inv}",
            superstep_id="ss-uncommitted",
            task_id=gate_task,
            attempt_id=f"{gate_task}-a-uncommitted",
            gate_report={
                "gate_id": "api-plan-review-gate",
                "verdict": "reject",
                "matched_rule": "reject_when",
                "reason": "uncommitted",
                "reads_sha256": {},
            },
        ).model_dump(mode="json")
    )
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    recovered = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    assert (
        recovered.gate_attempt.contract_digest == fixture.compiled.contract_digests["builtin:gate"]  # type: ignore[attr-defined]
    )


def test_recover_layer_inputs_last_committed_gate_wins(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    api_cycle_path = "main/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle"
    fixture._commit_cycle(
        invocation_id=fixture.api_cycle_inv,
        checkpoint_ns=f"ns/{fixture.api_cycle_inv}",
        structural_path=api_cycle_path,
        layer="api",
        applicable=True,
        include_decoy_gate=True,
    )
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    recovered = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    assert recovered.gate_report["verdict"] == "pass"


def test_recover_layer_inputs_tree_missing_never_uses_active_disk(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    profile = get_layer_assurance_profile("api")
    checks = _applicable_checks("api")
    checks_bytes = (json.dumps(checks.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    checks_path = fixture.change_dir / profile.checks_artifact
    checks_path.parent.mkdir(parents=True, exist_ok=True)
    checks_path.write_bytes(checks_bytes)
    review = _review("api")
    review_bytes = (json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    review_path = fixture.change_dir / profile.review_artifact
    review_path.write_bytes(review_bytes)
    dk_bytes = yaml.safe_dump(
        _data_knowledge().model_dump(mode="json"), sort_keys=False, allow_unicode=True
    ).encode("utf-8")
    (fixture.project / ".aa" / "data-knowledge.yaml").parent.mkdir(parents=True, exist_ok=True)
    (fixture.project / ".aa" / "data-knowledge.yaml").write_bytes(dk_bytes)

    store = TreeStore(fixture.change_dir)
    gate_tree = fixture.gate_trees["api"]

    def _missing_tree_only(tree_id: str, logical_path: str) -> bytes:
        if tree_id == gate_tree:
            raise FileNotFoundError(logical_path)
        return TreeStore.read_bytes(store, tree_id, logical_path)

    store.read_bytes = _missing_tree_only  # type: ignore[method-assign]
    with pytest.raises(ReplayBindingError, match="missing_evidence"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir, store=store)


def test_recover_layer_inputs_digest_drift_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    store = TreeStore(fixture.change_dir)
    profile = get_layer_assurance_profile("api")
    bad_bytes = b'{"schema_version":"2","status":"fail","checks":[]}\n'
    original_read = store.read_bytes

    def _drifted_read(tree_id: str, logical_path: str) -> bytes:
        payload = original_read(tree_id, logical_path)
        if logical_path.endswith(profile.checks_artifact):
            return bad_bytes
        return payload

    store.read_bytes = _drifted_read  # type: ignore[method-assign]
    with pytest.raises(ReplayBindingError, match="gate_evidence_drift"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir, store=store)


def test_recover_layer_inputs_mechanical_producer_unbound(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "task_attempt_succeeded" and payload.get("task_id", "").endswith(
            ":mechanical-plan-checks"
        ):
            payload = dict(payload)
            payload["outputs_sha256"] = {"change:review/api-plan-checks.json": "0" * 64}
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    with pytest.raises(ReplayBindingError, match="mechanical_producer_unbound"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_normalize_logical_path_strips_trailing_slash() -> None:
    assert normalize_logical_path("change:review/x.json") == normalize_logical_path("change:review/x.json/")


def test_bind_replay_definitions_non_terminal_root_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = [
        json.dumps(payload, sort_keys=True)
        for payload in lines
        if not (
            payload.get("type") in {"graph_completed", "graph_stopped", "graph_failed"}
            and payload.get("invocation_id") == _ROOT_INV
        )
    ]
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="root_invocation_unbound"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_missing_branch_wiring_defers_to_recovery(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    filtered = [
        payload
        for payload in lines
        if not (payload.get("type") == "graph_invocation_started" and payload.get("graph_id") == "api-branch")
    ]
    rewritten = []
    for seq, payload in enumerate(filtered, start=1):
        payload = dict(payload)
        payload["seq"] = seq
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.layer_topologies["api"].status == "wired"
    with pytest.raises(ReplayBindingError, match="ambiguous_graph_wiring"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_bind_replay_definitions_ambiguous_branch_wiring_defers_to_recovery(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    assurance_path = "main/assurance/assurance"
    api_branch_path = f"{assurance_path}/api/api-branch"
    duplicate = fixture._started(
        invocation_id="inv-api-branch-dup",
        entrypoint="api-branch",
        graph_id="api-branch",
        structural_path=api_branch_path,
        checkpoint_ns="dup-api-branch",
        params=dict(_PARAMS),
        parent_invocation_id=fixture.assurance_inv,
        parent_task_id=f"{assurance_path}:api",
    )
    fixture.append(duplicate.model_dump(mode="json"))
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    with pytest.raises(ReplayBindingError, match="ambiguous_graph_wiring"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_recover_layer_inputs_baseline_route_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "task_attempt_succeeded" and payload.get("task_id", "").endswith(
            ":review-gate"
        ):
            payload = dict(payload)
            gate_report = dict(payload.get("gate_report") or {})
            gate_report["verdict"] = "needs_human_review"
            gate_report["details"] = {"missing_capabilities": ["auth.api_admin_token"]}
            gate_report["value"] = "needs_human_review"
            payload["gate_report"] = gate_report
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    import assurance_agent.workflow.graph.replay_binding as replay_binding

    monkeypatch.setattr(replay_binding, "_assert_baseline_gate", lambda *args, **kwargs: None)
    with pytest.raises(ReplayBindingError, match="baseline_route_mismatch"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_recover_layer_inputs_baseline_gate_details_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "task_attempt_succeeded" and payload.get("task_id", "").endswith(
            ":review-gate"
        ):
            payload = dict(payload)
            gate_report = dict(payload.get("gate_report") or {})
            gate_report["details"] = {"missing_capabilities": ["auth.api_admin_token"]}
            payload["gate_report"] = gate_report
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    with pytest.raises(ReplayBindingError, match="baseline_gate_mismatch"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_recover_layer_inputs_ignores_failed_abandoned_gate_attempts(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    api_cycle_path = "main/assurance/assurance/api/api-branch/review-cycle/api-plan-cycle"
    gate_task = f"{api_cycle_path}:review-gate"
    failed_attempt = f"{gate_task}-a-failed"
    abandoned_attempt = f"{gate_task}-a-abandoned"
    checkpoint_ns = f"ns/{fixture.api_cycle_inv}"

    for attempt_id, verdict in ((failed_attempt, "reject"), (abandoned_attempt, "needs_human_review")):
        ss = f"{fixture.api_cycle_inv}-ss-{attempt_id}"
        fixture.append(
            SuperstepPlannedEvent(
                type="superstep_planned",
                invocation_id=fixture.api_cycle_inv,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                checkpoint_id=f"{fixture.api_cycle_inv}-cp-{attempt_id}",
                task_ids=[gate_task],
            ).model_dump(mode="json")
        )
        fixture.append(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id=fixture.api_cycle_inv,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                task_id=gate_task,
                attempt_id=attempt_id,
                node_id="review-gate",
                input_sha256="in",
                graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
                contract_digest="gate-contract-bad",
                attempt_number=1,
                lease_expires_at="2026-07-31T00:00:00Z",
                started_at="2026-07-31T00:00:00Z",
            ).model_dump(mode="json")
        )
        fixture.append(
            TaskAttemptSucceededEvent(
                type="task_attempt_succeeded",
                invocation_id=fixture.api_cycle_inv,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                task_id=gate_task,
                attempt_id=attempt_id,
                gate_report={
                    "gate_id": "api-plan-review-gate",
                    "verdict": verdict,
                    "matched_rule": "reject_when",
                    "reason": attempt_id,
                    "reads_sha256": {},
                },
            ).model_dump(mode="json")
        )
        fixture.append(
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id=fixture.api_cycle_inv,
                checkpoint_ns=checkpoint_ns,
                superstep_id=ss,
                checkpoint_id=f"{fixture.api_cycle_inv}-cp-{attempt_id}-commit",
                write_set_ids=[],
                target_tree_id=fixture.gate_trees["api"],
                state_values={},
                committed_task_ids=[gate_task],
            ).model_dump(mode="json")
        )
        if attempt_id == failed_attempt:
            fixture.append(
                {
                    "type": "task_attempt_failed",
                    "invocation_id": fixture.api_cycle_inv,
                    "checkpoint_ns": checkpoint_ns,
                    "superstep_id": ss,
                    "task_id": gate_task,
                    "attempt_id": attempt_id,
                    "error_kind": "contract",
                    "message": "failed gate",
                }
            )
        else:
            fixture.append(
                {
                    "type": "task_attempt_abandoned",
                    "invocation_id": fixture.api_cycle_inv,
                    "checkpoint_ns": checkpoint_ns,
                    "task_id": gate_task,
                    "attempt_id": attempt_id,
                    "reason": "abandoned gate",
                    "abandoned_at": "2026-07-31T00:00:00Z",
                }
            )

    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    recovered = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    assert recovered.gate_report["verdict"] == "pass"
    assert (
        recovered.gate_attempt.contract_digest == fixture.compiled.contract_digests["builtin:gate"]  # type: ignore[attr-defined]
    )


def test_frozen_definition_digests_match_runtime() -> None:
    assert gate_semantics_digest()
    assert assurance_profile_digest()


def _root_started_from_fixture(fixture: ReplayBindingFixture) -> GraphInvocationStartedEvent:
    return fixture._started(
        invocation_id=_ROOT_INV,
        entrypoint=_ENTRYPOINT,
        graph_id="main",
        structural_path="main",
        checkpoint_ns=_ROOT_INV,
        params=dict(_PARAMS),
        root_tree_id=fixture.root_tree,
    )


def test_bind_replay_definitions_pinned_ingest_catalog_missing(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    catalog_path = (
        fixture.change_dir
        / ".graph-runtime"
        / "ingest-catalogs"
        / f"{fixture.compiled.ingest_catalog_digest}.json"  # type: ignore[attr-defined]
    )
    catalog_path.unlink()
    with pytest.raises(ReplayBindingError, match="pinned_ingest_catalog_missing"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_ingest_catalog_invalid(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    catalog_path = (
        fixture.change_dir
        / ".graph-runtime"
        / "ingest-catalogs"
        / f"{fixture.compiled.ingest_catalog_digest}.json"  # type: ignore[attr-defined]
    )
    catalog_path.write_bytes(b"{not-json")
    with pytest.raises(ReplayBindingError, match="pinned_ingest_catalog_invalid"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_ingest_catalog_digest_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    catalog_path = (
        fixture.change_dir
        / ".graph-runtime"
        / "ingest-catalogs"
        / f"{fixture.compiled.ingest_catalog_digest}.json"  # type: ignore[attr-defined]
    )
    other = validate_catalog_runtime().model_copy(
        update={
            "artifacts": {
                "only": validate_catalog_runtime()
                .artifacts["api_plan"]
                .model_copy(update={"path": "change:plans/other.md"})
            }
        }
    )
    catalog_path.write_bytes(
        (
            json.dumps(
                other.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    with pytest.raises(ReplayBindingError, match="pinned_ingest_catalog_digest_mismatch"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_contract_snapshot_missing(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    target, digest = next(iter(fixture.compiled.contract_digests.items()))  # type: ignore[attr-defined]
    del target
    (fixture.change_dir / ".graph-runtime" / "contracts" / f"{digest}.json").unlink()
    with pytest.raises(ReplayBindingError, match="pinned_contract_snapshot_missing"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_contract_digest_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    target, digest = next(iter(fixture.compiled.contract_digests.items()))  # type: ignore[attr-defined]
    contract = fixture.contracts.contracts[target]  # type: ignore[attr-defined]
    mutated = contract.model_copy(update={"reads": (*contract.reads, "change:plans/extra.md")})
    path = fixture.change_dir / ".graph-runtime" / "contracts" / f"{digest}.json"
    path.write_bytes(
        (
            json.dumps(
                mutated.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    with pytest.raises(ReplayBindingError, match="pinned_contract_digest_mismatch"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_pinned_contract_target_mismatch(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    target, digest = next(iter(fixture.compiled.contract_digests.items()))  # type: ignore[attr-defined]
    contract = fixture.contracts.contracts[target]  # type: ignore[attr-defined]
    wrong_target = "operation:not-the-recorded-target"
    mutated = contract.model_copy(update={"target": wrong_target})
    path = fixture.change_dir / ".graph-runtime" / "contracts" / f"{digest}.json"
    path.write_bytes(
        (
            json.dumps(
                mutated.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    with pytest.raises(ReplayBindingError, match="pinned_contract_target_mismatch"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_rejects_unrecorded_referenced_target(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    target, digest = next(iter(sorted(fixture.compiled.contract_digests.items())))  # type: ignore[attr-defined]
    (fixture.change_dir / ".graph-runtime" / "contracts" / f"{digest}.json").unlink()
    # Drop the recorded digest from the root event so the referenced target is unrecorded.
    events = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in events:
        payload = json.loads(line)
        if payload.get("invocation_id") == _ROOT_INV and payload.get("type") == "graph_invocation_started":
            digests = dict(payload["contract_digests"])
            digests.pop(target)
            payload["contract_digests"] = digests
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="pinned_contract|unrecorded|no execution contract"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_rejects_extra_conflicting_binding(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    extra_target = "operation:extra-conflicting-binding"
    extra_contract = next(iter(fixture.contracts.contracts.values())).model_copy(  # type: ignore[attr-defined]
        update={"target": extra_target}
    )
    extra_digest = canonical_digest(extra_contract)
    payload = (
        json.dumps(
            extra_contract.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    (fixture.change_dir / ".graph-runtime" / "contracts" / f"{extra_digest}.json").write_bytes(payload)
    events = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in events:
        payload_obj = json.loads(line)
        if (
            payload_obj.get("invocation_id") == _ROOT_INV
            and payload_obj.get("type") == "graph_invocation_started"
        ):
            digests = dict(payload_obj["contract_digests"])
            digests[extra_target] = extra_digest
            payload_obj["contract_digests"] = digests
        rewritten.append(json.dumps(payload_obj, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="pinned_contract|conflicting|digest"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_ignores_current_ingest_model_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    started = _root_started_from_fixture(fixture)

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("replay must not consult current ingest model registry")

    monkeypatch.setattr("assurance_agent.workflow.graph.ingest_catalog.resolve_model", explode)
    monkeypatch.setattr("assurance_agent.workflow.graph.ingest_catalog.validate_catalog_runtime", explode)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id="CH-PINNED-001",
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
        store=TreeStore(fixture.change_dir),
    )
    assert binding.compiled.ingest_catalog_digest == started.ingest_catalog_digest


def test_bind_replay_definitions_ignores_current_execution_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    expected = dict(fixture.compiled.contract_digests)  # type: ignore[attr-defined]

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("replay must not load current execution contracts")

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.contracts.load_execution_contracts",
        explode,
    )
    # Mutate project-local contracts after pinning.
    local = fixture.project / ".aa" / "execution-contracts.yaml"
    local.write_text(
        "schema_version: '1'\ncontracts:\n  operation:mutated:\n    handler: operation\n",
        encoding="utf-8",
    )
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.compiled.contract_digests == expected


def test_load_pinned_execution_definition_succeeds_when_current_loaders_explode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    request = PinnedDefinitionRequest(
        graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
        ingest_catalog_digest=fixture.compiled.ingest_catalog_digest,  # type: ignore[attr-defined]
        contract_digests=tuple(sorted(fixture.compiled.contract_digests.items())),  # type: ignore[attr-defined]
        event_schema_version=4,
        gate_semantics_digest=fixture.binding.gate_semantics_digest,  # type: ignore[attr-defined]
        assurance_profile_digest=fixture.binding.assurance_profile_digest,  # type: ignore[attr-defined]
    )

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pinned loader must not consult current catalogs")

    monkeypatch.setattr("assurance_agent.workflow.graph.ingest_catalog.validate_catalog_runtime", explode)
    monkeypatch.setattr("assurance_agent.workflow.graph.ingest_catalog.resolve_model", explode)
    monkeypatch.setattr("assurance_agent.workflow.graph.contracts.load_execution_contracts", explode)
    resolved = load_pinned_execution_definition(fixture.change_dir, request)
    assert resolved.compiled.digest == request.graph_digest
    assert resolved.compiled.ingest_catalog_digest == request.ingest_catalog_digest
    assert resolved.compiled.contract_digests == dict(request.contract_digests)


def test_load_pinned_execution_definition_missing_snapshot_fails_closed(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    request = PinnedDefinitionRequest(
        graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
        ingest_catalog_digest=fixture.compiled.ingest_catalog_digest,  # type: ignore[attr-defined]
        contract_digests=tuple(sorted(fixture.compiled.contract_digests.items())),  # type: ignore[attr-defined]
        event_schema_version=4,
        gate_semantics_digest=fixture.binding.gate_semantics_digest,  # type: ignore[attr-defined]
        assurance_profile_digest=fixture.binding.assurance_profile_digest,  # type: ignore[attr-defined]
    )
    catalog_path = (
        fixture.change_dir / ".graph-runtime" / "ingest-catalogs" / f"{request.ingest_catalog_digest}.json"
    )
    catalog_path.unlink()
    with pytest.raises(PinnedDefinitionError, match="pinned_ingest_catalog_missing"):
        load_pinned_execution_definition(fixture.change_dir, request)


def test_pinned_reason_for_compile_error_maps_digest_and_structural_failures() -> None:
    assert (
        _pinned_reason_for_compile_error(
            CompileError("historical compile identity mismatch: ingest_catalog_digest expected abc, got def")
        )
        == "pinned_ingest_catalog_digest_mismatch"
    )
    assert (
        _pinned_reason_for_compile_error(
            CompileError(
                "historical compile identity mismatch: contract 'operation:x' digest expected a, got b"
            )
        )
        == "pinned_contract_digest_mismatch"
    )
    assert (
        _pinned_reason_for_compile_error(
            CompileError("workflow v2 compile failed:\n  - unknown node 'missing'")
        )
        == "pinned_schema_compile_failed"
    )


def test_load_pinned_execution_definition_maps_structural_compile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    request = PinnedDefinitionRequest(
        graph_digest=fixture.compiled.digest,  # type: ignore[attr-defined]
        ingest_catalog_digest=fixture.compiled.ingest_catalog_digest,  # type: ignore[attr-defined]
        contract_digests=tuple(sorted(fixture.compiled.contract_digests.items())),  # type: ignore[attr-defined]
        event_schema_version=4,
        gate_semantics_digest=fixture.binding.gate_semantics_digest,  # type: ignore[attr-defined]
        assurance_profile_digest=fixture.binding.assurance_profile_digest,  # type: ignore[attr-defined]
    )

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.compiler.validate_historical_replay_surface",
        lambda _schema: ("historical replay surface is not valid",),
    )
    with pytest.raises(PinnedDefinitionError) as exc_info:
        load_pinned_execution_definition(fixture.change_dir, request)
    assert exc_info.value.reason_code == "pinned_schema_compile_failed"


def test_bind_replay_definitions_maps_structural_compile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_fixture(tmp_path)
    monkeypatch.setattr(
        "assurance_agent.workflow.graph.compiler.validate_historical_replay_surface",
        lambda _schema: ("historical replay surface is not valid",),
    )
    with pytest.raises(ReplayBindingError) as exc_info:
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )
    assert exc_info.value.reason_code == "pinned_schema_compile_failed"


def _stage_profile_snapshot(change_dir: Path, digest: str, data: bytes) -> None:
    path = change_dir / ".graph-runtime" / "assurance-profiles" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


_V5_PROFILE_SPY_CALLS: list[str] = []


def _v5_profile_spy(layer: str) -> object:
    _V5_PROFILE_SPY_CALLS.append(layer)
    raise AssertionError("v5 topology binding must not call get_layer_assurance_profile")


def test_bind_replay_definitions_v5_missing_profile_snapshot(tmp_path: Path) -> None:
    from assurance_agent.verification.profile_manifest import assurance_profile_bytes

    fixture = _build_fixture(tmp_path)
    profile_bytes = assurance_profile_bytes()
    digest = hashlib.sha256(profile_bytes).hexdigest()
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["event_schema_version"] = 5
            payload["assurance_profile_digest"] = digest
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="profile_snapshot_missing"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_bind_replay_definitions_v5_tampered_profile_snapshot(tmp_path: Path) -> None:
    from assurance_agent.verification.profile_manifest import assurance_profile_bytes

    fixture = _build_fixture(tmp_path)
    profile_bytes = assurance_profile_bytes()
    digest = hashlib.sha256(profile_bytes).hexdigest()
    _stage_profile_snapshot(fixture.change_dir, digest, profile_bytes[:-2] + b"x\n")
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["event_schema_version"] = 5
            payload["assurance_profile_digest"] = digest
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with pytest.raises(ReplayBindingError, match="profile_snapshot_digest_mismatch"):
        bind_replay_definitions(
            change_dir=fixture.change_dir,
            change_id=_CHANGE_ID,
            root_invocation_id=_ROOT_INV,
            expected_entrypoint=_ENTRYPOINT,
        )


def test_v5_topology_specs_ignore_current_profile_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.verification.profile_manifest import (
        assurance_profile_bytes,
        parse_assurance_profile_snapshot,
    )
    from assurance_agent.workflow.graph.replay_schema import LayerTopologySpec

    fixture = _build_fixture(tmp_path)
    profile_bytes = assurance_profile_bytes()
    digest = hashlib.sha256(profile_bytes).hexdigest()
    _stage_profile_snapshot(fixture.change_dir, digest, profile_bytes)
    lines = fixture.events_path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        payload = json.loads(line)
        if payload.get("type") == "graph_invocation_started":
            payload["event_schema_version"] = 5
            payload["assurance_profile_digest"] = digest
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    first = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    first_specs = first.layer_topology_specs
    first_statuses = {layer: topo.status for layer, topo in first.layer_topologies.items()}

    _V5_PROFILE_SPY_CALLS.clear()
    monkeypatch.setattr(
        "assurance_agent.workflow.graph.replay_schema.get_layer_assurance_profile",
        _v5_profile_spy,
    )
    second = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert second.layer_topology_specs == first_specs
    assert {layer: topo.status for layer, topo in second.layer_topologies.items()} == first_statuses
    assert all(isinstance(spec, LayerTopologySpec) for spec in second.layer_topology_specs)
    assert _V5_PROFILE_SPY_CALLS == []

    manifest = parse_assurance_profile_snapshot(profile_bytes)
    mutated_profiles = []
    for entry in manifest.profiles:
        if entry.layer in {"fuzz", "performance"}:
            mutated_profiles.append(
                entry.model_copy(
                    update={
                        "plan_artifacts": ("plans/mutated.md",),
                        "review_artifact": "review/mutated-review.json",
                        "review_alias": "mutated_review",
                        "checks_artifact": "review/mutated-checks.json",
                        "gate_id": f"{entry.layer}-mutated-gate",
                    }
                )
            )
        else:
            mutated_profiles.append(entry)
    mutated_manifest = manifest.model_copy(update={"profiles": tuple(mutated_profiles)})
    mutated_bytes = (
        json.dumps(
            mutated_manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    monkeypatch.setattr(
        "assurance_agent.workflow.graph.replay_binding.assurance_profile_bytes",
        lambda: mutated_bytes,
    )
    third = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert third.layer_topology_specs == first_specs
    assert third.profile_compatibility["fuzz"] is False
    assert third.profile_compatibility["performance"] is False
    assert third.profile_compatibility["api"] is True


def test_recover_rejects_foreign_definition_epoch_child(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "graph_invocation_started" and payload.get("graph_id") == "api-branch":
            payload = dict(payload)
            payload["assurance_profile_digest"] = "f" * 64
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    with pytest.raises(ReplayBindingError, match="ambiguous_graph_wiring"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_recover_ignores_active_file_mutations(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    before = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    profile = get_layer_assurance_profile("api")
    (fixture.change_dir / profile.checks_artifact).parent.mkdir(parents=True, exist_ok=True)
    (fixture.change_dir / profile.checks_artifact).write_text('{"mutated":true}\n', encoding="utf-8")
    (fixture.change_dir / profile.review_artifact).write_text('{"mutated":true}\n', encoding="utf-8")
    (fixture.project / ".aa" / "data-knowledge.yaml").write_text("mutated: true\n", encoding="utf-8")
    (fixture.project / ".aa" / "policy.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    after = recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)
    assert after.checks.raw_bytes == before.checks.raw_bytes
    assert after.review is not None and before.review is not None
    assert after.review.raw_bytes == before.review.raw_bytes
    assert after.data_knowledge is not None and before.data_knowledge is not None
    assert after.data_knowledge.raw_bytes == before.data_knowledge.raw_bytes
    assert after.gate_report == before.gate_report


def test_recover_requires_pinned_contract_digest(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, include_e2e=False)
    lines = [json.loads(line) for line in fixture.events_path.read_text(encoding="utf-8").splitlines()]
    rewritten = []
    for payload in lines:
        if payload.get("type") == "task_attempt_started" and str(payload.get("node_id")) == "review-gate":
            payload = dict(payload)
            payload["contract_digest"] = "0" * 64
        rewritten.append(json.dumps(payload, sort_keys=True))
    fixture.events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    with pytest.raises(ReplayBindingError, match="no_successful_gate_evidence"):
        recover_layer_inputs(binding, layer="api", change_dir=fixture.change_dir)


def test_v4_activated_specialty_without_snapshot_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.workflow.graph import replay_binding as replay_mod
    from assurance_agent.workflow.graph.replay_schema import (
        PinnedLayerTopology,
        classify_pinned_layer_topology,
    )

    fixture = _build_fixture(tmp_path, include_e2e=False)
    original = classify_pinned_layer_topology

    def classify_force_wired(schema, topology_spec):  # type: ignore[no-untyped-def]
        topology = original(schema, topology_spec)
        if topology_spec.layer == "fuzz":
            return PinnedLayerTopology(
                layer="fuzz",
                status="wired",
                assurance_node_id="fuzz",
                branch_graph_id="fuzz-branch",
                cycle_call_node_id="review-cycle",
                cycle_graph_id="fuzz-plan-review-cycle",
                applicability_node_id="applicability",
                reviewer_node_id="review",
                mechanical_node_id="mechanical-plan-checks",
                gate_node_id="review-gate",
                human_review_node_id="human-review",
                knowledge_remediation_node_id="knowledge-remediation",
                codegen_precondition_node_id="codegen-precheck",
                codegen_node_id="codegen",
                diagnostics=(),
            )
        return topology

    monkeypatch.setattr(replay_mod, "classify_pinned_layer_topology", classify_force_wired)
    binding = bind_replay_definitions(
        change_dir=fixture.change_dir,
        change_id=_CHANGE_ID,
        root_invocation_id=_ROOT_INV,
        expected_entrypoint=_ENTRYPOINT,
    )
    assert binding.layer_topologies["fuzz"].status == "partial"
    assert any("without profile snapshot" in item for item in binding.layer_topologies["fuzz"].diagnostics)
    with pytest.raises(ReplayBindingError, match="partial_assurance_wiring"):
        recover_layer_inputs(binding, layer="fuzz", change_dir=fixture.change_dir)
