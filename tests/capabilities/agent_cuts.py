"""Indeterminate agent-cut helpers for capability finalize handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, cast

from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue
from tests.capabilities.conformance import ExecutedTask, execute_task
from tests.capabilities.six_wheel_harness import (
    ADAPTER_IDS,
    SixWheelTaskHost,
    _FixtureNodeOutput,
    _application_call,
    _import_activation,
    _start_until_blocked,
    resolve_fixture,
)
from tests.acg_plan_fixture import install_plan

from assurance_generation.operations.planning import PlanFinalizeHandler
from assurance_healing.agent_ops.fix_proposal import finalize as fix_proposal_finalize
from assurance_improvement.agent_ops.retro import finalize as retro_finalize
from assurance_intake.agent_ops.case_review import finalize as case_review_finalize
from assurance_quality.agent_ops.inspect import finalize as inspect_finalize

AGENT_CUTS = (
    "prepare-complete",
    "dispatch-unknown",
    "bound-running",
    "result-truncated",
    "terminal-observed",
)
WHEEL_FINALIZERS = {
    "intake": case_review_finalize,
    "generation": lambda: PlanFinalizeHandler("api"),
    "healing": fix_proposal_finalize,
    "quality": inspect_finalize,
    "improvement": retro_finalize,
}

_HEX = "a" * 64
_PLAN_REF = {
    "path": f"qa/results/plan/{_HEX}/resolved-assurance-plan.json",
    "digest": _HEX,
}


@dataclass
class IndeterminateObservation:
    status: str
    failure_kind: str | None
    effects: tuple[object, ...]
    stop_reason: str | None
    workspace_bytes: dict[str, bytes]
    finalize_invoked: bool
    finalize_failed_closed: bool
    spawned: bool = False


class CuttingTaskHost(SixWheelTaskHost):
    def __init__(self, *, adapter_id: str, provider_state_dir: Path, cut: str) -> None:
        super().__init__(adapter_id=adapter_id, provider_state_dir=provider_state_dir)
        self.cut = cut
        self.finalize_calls = 0
        self.spawned = False

    async def execute_cut(self, validated_input: object, context: object) -> _FixtureNodeOutput:
        del validated_input, context
        self.spawned = self.cut != "prepare-complete"
        raise ValueError(f"indeterminate cut {self.cut}")

    async def finalize_cut(self, validated_input: object, context: object) -> _FixtureNodeOutput:
        del validated_input, context
        self.finalize_calls += 1
        raise ValueError(f"finalize blocked at {self.cut}")


def _handler(wheel: str) -> object:
    factory = WHEEL_FINALIZERS[wheel]
    if isinstance(factory, ModuleType):
        return factory
    return factory()


def _cut_payload(wheel: str, cut: str) -> JSONValue:
    base: dict[str, Any] = {
        "capability_leafs": ["auth.session.create", "entities.item.create"],
        "artifact_paths": [],
        "plan_digest": _HEX,
        "plan_ref": _PLAN_REF,
    }
    if wheel == "generation":
        base["allowed_paths"] = []
    if wheel == "execution":
        base.update(
            {
                "change_id": "CH-DEMO-001",
                "batch_id": "batch-1",
                "case_ids": ["TC-1"],
                "selected_targets": {
                    "api": False,
                    "e2e": False,
                    "fuzz": False,
                    "performance": False,
                },
                "baseline_tree_id": _HEX,
                "runner_profile_digest": _HEX,
                "execution_view_root": ("qa/.staging/task/attempt-1/qa/.staging/execution/batch-1"),
                "execution_view_digest": _HEX,
                "executed_at": "2026-09-05T00:00:00Z",
                "mapping": {
                    "schema_version": "1",
                    "selected": [],
                    "mappings": [],
                },
            }
        )
    if wheel == "healing":
        prepare = {
            "change_id": "CH-DEMO-001",
            "plan_digest": _HEX,
            "plan_ref": _PLAN_REF,
            "owner_id": "assurance.healing",
            "capability_leafs": list(base["capability_leafs"]),
            "allowed_paths": ["tests/api/test_users.py"],
            "allowed_roots": ["tests/"],
            "baseline_digest": _HEX,
            "candidate_digest": _HEX,
            "policy_digest": _HEX,
            "mapping_paths": ["tests/api/test_users.py"],
            "require_approval": False,
            "execution_evidence_digest": _HEX,
            "claimed_capabilities": [],
        }
        base = {**prepare, "prepare": prepare}
    if wheel == "quality":
        base.update(
            {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": 0,
                "batch_id": "batch-1",
                "assessment": {
                    "change_id": "CH-DEMO-001",
                    "coverage_epoch": 0,
                    "batch_id": "batch-1",
                    "plan_digest": _HEX,
                    "plan_ref": _PLAN_REF,
                    "scope": {
                        "change_id": "CH-DEMO-001",
                        "coverage_epoch": 0,
                        "required_case_ids": ["TC-1"],
                        "selected_families": ["api"],
                        "applicable_goals": ["constraint_coverage"],
                        "applicability_refs": [{"path": "qa/cases/api/case.yaml", "digest": _HEX}],
                        "risk_tier": "low",
                        "policy_digest": _HEX,
                    },
                    "policy": {
                        "coverage_floor_by_tier": {
                            "low": 0.8,
                            "medium": 0.8,
                            "high": 0.8,
                            "critical": 0.8,
                        }
                    },
                    "trace_ref": {
                        "path": "qa/results/inspect/trace.json",
                        "digest": _HEX,
                    },
                    "gaps_ref": {
                        "path": "qa/results/inspect/gaps.json",
                        "digest": _HEX,
                    },
                    "metrics_ref": {
                        "path": "qa/results/inspect/metrics.json",
                        "digest": _HEX,
                    },
                    "sufficiency_ref": {
                        "path": "qa/results/inspect/sufficiency.json",
                        "digest": _HEX,
                    },
                    "execution_ref": {
                        "path": "qa/results/execution/result.json",
                        "digest": _HEX,
                    },
                    "observations_ref": {
                        "path": "qa/results/inspect/observations.json",
                        "digest": _HEX,
                    },
                    "obligation_assessment_ref": {
                        "path": "qa/results/inspect/obligation-assessment.json",
                        "digest": _HEX,
                    },
                    "obligation_gate_facts": {
                        "required_count": 1,
                        "supported_count": 1,
                        "refuted_count": 0,
                        "inconclusive_count": 0,
                        "repairable_gap_count": 0,
                        "human_gap_count": 0,
                    },
                    "issue_evidence_manifest_ref": {
                        "path": "qa/results/inspect/issue-evidence-manifest.json",
                        "digest": _HEX,
                    },
                    "owned_evidence_ids": [],
                    "evidence_bundle_digest": f"sha256:{_HEX}",
                },
                "reviewed_case": {
                    "change_id": "CH-DEMO-001",
                    "coverage_epoch": 0,
                    "plan_digest": _HEX,
                    "plan_ref": _PLAN_REF,
                    "preparation_refs": [
                        {"path": "qa/results/intake/prepare.json", "digest": _HEX},
                        _PLAN_REF,
                    ],
                    "case_refs": [{"path": "qa/cases/api/case.yaml", "digest": _HEX}],
                    "review_ref": {
                        "path": "qa/results/review/case-review.json",
                        "digest": _HEX,
                    },
                    "selection_ref": {
                        "path": "qa/results/cases/epochs/0/selection.json",
                        "digest": _HEX,
                    },
                },
                "mapping_ref": {
                    "path": "qa/results/generated/mapping.json",
                    "digest": _HEX,
                },
                "fact_baseline_ref": {
                    "path": "qa/results/facts/fact-baseline.json",
                    "digest": _HEX,
                },
            }
        )
    if wheel == "improvement":
        base = {
            "change_id": "CH-DEMO-001",
            "context": {
                "schema_version": "3",
                "retro_id": "RET-1",
                "generated_at": "2026-09-09T00:00:00Z",
                "window": {
                    "selection": {
                        "mode": "change_ids",
                        "requested_change_ids": ["CH-DEMO-001"],
                    },
                    "change_ids": ["CH-DEMO-001"],
                },
                "source_manifest": {
                    "issue_slice_sha256": _HEX,
                    "workflow_slice_sha256": _HEX,
                    "eval_slice_sha256": _HEX,
                    "issue_sources": [],
                    "workflow_sources": [],
                    "eval_sources": [],
                },
                "integrity": {"status": "complete", "reasons": []},
                "domain_status": {
                    "issue": {"status": "ok"},
                    "workflow": {"status": "ok"},
                    "eval": {"status": "ok"},
                },
                "signals": {"issue": [], "workflow": [], "eval": []},
                "signal_count": 0,
            },
        }
    if cut == "prepare-complete":
        return cast(JSONValue, base)
    if cut == "dispatch-unknown":
        base["agent_result"] = None
        return cast(JSONValue, base)
    if cut == "bound-running":
        base["agent_result"] = {"status": "running", "adapter_id": "test.fake"}
        return cast(JSONValue, base)
    if cut == "result-truncated":
        base["agent_result"] = {"schema_version": "1", "result_payload": {}}
        return cast(JSONValue, base)
    structured = {"ok": True}
    base["agent_result"] = {
        "schema_version": "1",
        "result_payload": structured,
        "result_digest": canonical_digest(structured),
        "evidence_digest": _HEX,
        "adapter_id": "test.fake",
        "adapter_version": "1.0.0",
    }
    return cast(JSONValue, base)


async def run_finalize_cut(wheel: str, cut: str) -> IndeterminateObservation:
    handler = _handler(wheel)
    with TemporaryDirectory(prefix="capabilities-indeterminate-") as temporary:
        workspace = Path(temporary)
        marker = workspace / "outside-must-not-appear.txt"
        payload = _cut_payload(wheel, cut)
        if wheel == "intake":
            plan, plan_ref = install_plan(
                workspace,
                "CH-DEMO-001",
                capability_leafs=("auth.session.create", "entities.item.create"),
            )
            payload = cast(
                JSONValue,
                {**cast(dict[str, Any], payload), "plan_digest": plan.plan_digest, "plan_ref": plan_ref},
            )
        executed = await execute_task(cast(Any, handler), payload, workspace)
        return _from_executed(executed, workspace, marker.exists())


async def run_six_wheel_cut(cut: str) -> IndeterminateObservation:
    resolved = resolve_fixture("six-wheel-opencode")
    host = CuttingTaskHost(
        adapter_id=ADAPTER_IDS["six-wheel-opencode"],
        provider_state_dir=resolved.workspace / f"cut-{cut}-provider",
        cut=cut,
    )
    engine_root = resolved.workspace / f"cut-{cut}-engine"
    invocation_root = engine_root / "leases"
    status = "failed"
    with _import_activation(resolved.product_root, resolved.workspace):
        try:
            result, invocation_root, *_rest = _application_call(
                _start_until_blocked,
                engine_root,
                host,
                resolved.composition,
                f"capabilities-cut-{cut}",
                execute=host.execute_cut,
                finalize=host.finalize_cut,
            )
            status = result.status
        except Exception:
            status = "failed"
    workspace_bytes = {
        path.relative_to(invocation_root).as_posix(): path.read_bytes()
        for path in invocation_root.rglob("*")
        if invocation_root.exists() and path.is_file() and not path.is_symlink()
    }
    failed_closed = status not in {"succeeded", "completed"} and host.finalize_calls == 0
    if host.finalize_calls and status not in {"succeeded", "completed"}:
        failed_closed = True
    return IndeterminateObservation(
        status=status if status != "completed" else "failed",
        failure_kind=None if status != "completed" else None,
        effects=(),
        stop_reason=None,
        workspace_bytes=workspace_bytes,
        finalize_invoked=host.finalize_calls > 0,
        finalize_failed_closed=failed_closed,
        spawned=host.spawned and cut != "prepare-complete",
    )


def assert_indeterminate_is_inert(observed: IndeterminateObservation) -> None:
    if isinstance(observed, ExecutedTask):
        raise TypeError("expected IndeterminateObservation")
    assert observed.effects == ()
    assert observed.status != "succeeded"
    assert observed.status != "stopped"
    if observed.failure_kind is not None:
        assert observed.failure_kind in {"invalid_input", "invalid_output"}
        assert observed.status == "failed"
    business = [
        name
        for name in observed.workspace_bytes
        if name.endswith(("review.json", "proposal.json", "report.json"))
        or "effect" in name
        or name.endswith(".stop")
    ]
    assert business == []


def _from_executed(executed: ExecutedTask, workspace: Path, leaked: bool) -> IndeterminateObservation:
    del workspace
    assert leaked is False
    failure = executed.failure
    return IndeterminateObservation(
        status=executed.status,
        failure_kind=None if failure is None else failure.kind,
        effects=tuple(executed.effects),
        stop_reason=executed.stop_reason,
        workspace_bytes=dict(executed.workspace_bytes),
        finalize_invoked=True,
        finalize_failed_closed=executed.status == "failed",
    )
