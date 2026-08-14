"""Test-only harness for the real four-layer codegen-only GraphRuntime matrix.

Supplies agent output and coordinator fault seams only. Does not implement gates,
topology, planner decisions, or write authorization.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.artifacts.models.generated_files import GeneratedFilesV1
from assurance_agent.verification.generated_files import (
    get_generated_files_contract,
    get_generated_files_model,
)
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.runtime_factory import (
    RuntimeBundle,
    build_graph_runtime,
    runtime_context_for,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import parse_import_manifest
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    ImportResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from tests.helpers_aa import write_aa_config
from tests.helpers_assurance_contract import load_canonical_assurance_bundle

CHANGE_ID = "CH-CANONICAL"
LAYERS: tuple[LayerName, ...] = ("api", "e2e", "fuzz", "performance")
_FIXTURE_ID = "four-layer-runtime"
_PLAN_SKILLS = frozenset(
    {
        "skill:aa-api-plan",
        "skill:aa-e2e-plan",
        "skill:aa-fuzz-plan",
        "skill:aa-performance-plan",
    }
)
_REVIEWER_BY_LAYER: dict[str, str] = {
    "api": "skill:aa-api-plan-reviewer",
    "e2e": "skill:aa-e2e-plan-reviewer",
    "fuzz": "skill:aa-fuzz-plan-reviewer",
    "performance": "skill:aa-performance-plan-reviewer",
}
_CODEGEN_BY_LAYER: dict[str, str] = {
    "api": "skill:aa-api-codegen",
    "e2e": "skill:aa-e2e-codegen",
    "fuzz": "skill:aa-fuzz-codegen",
    "performance": "skill:aa-performance-codegen",
}
_PLAN_FIXER_BY_LAYER: dict[str, str] = {
    "api": "skill:aa-api-plan-fixer",
    "e2e": "skill:aa-e2e-plan-fixer",
}
_CODEGEN_FIXER_BY_LAYER: dict[str, str] = {
    "api": "skill:aa-api-codegen-fixer",
    "e2e": "skill:aa-e2e-codegen-fixer",
}
_LAYER_BY_TARGET = {
    **{target: layer for layer, target in _REVIEWER_BY_LAYER.items()},
    **{target: layer for layer, target in _CODEGEN_BY_LAYER.items()},
    **{target: layer for layer, target in _PLAN_FIXER_BY_LAYER.items()},
    **{target: layer for layer, target in _CODEGEN_FIXER_BY_LAYER.items()},
    "skill:aa-fix-proposal": "api",
}
ReviewScript = Literal[
    "pass",
    "needs_fix_auto",
    "needs_fix_human",
    "needs_fix_human_missing_capability",
    "knowledge_gap",
]
NODE_CHAIN_AFTER = {
    "review": "mechanical-plan-checks",
    "mechanical-plan-checks": "review-gate",
    "review-gate": "codegen-precheck",
    "codegen-precheck": "codegen",
}
_CODEGEN_TEST_BY_LAYER: dict[str, tuple[str, str, str]] = {
    # case_id, repo_path, symbol
    "api": (
        "API-ACC-001",
        "tests/api/test_accounts_api.py",
        "test_api_acc_001__create_account_success",
    ),
    "e2e": (
        "TC_E2E_AUTH_REJECT",
        "tests/e2e/test_api_management_e2e.py",
        "test_tc_e2e_auth_reject__limited_user_denied",
    ),
    "fuzz": (
        "FUZZ-001",
        "tests/fuzz/test_accounts_fuzz.py",
        "test_fuzz_001__account_create_schema",
    ),
    "performance": (
        "PERF-001",
        "tests/perf/locustfile_accounts.py",
        "get_accounts",
    ),
}
_REVIEW_SUMMARY_BY_LAYER = {
    "api": "review/api-plan-review-summary.md",
    "e2e": "review/plan-review-summary.md",
    "fuzz": "review/fuzz-plan-review-summary.md",
    "performance": "review/performance-plan-review-summary.md",
}
MutationName = Literal[
    "none",
    "missing_review",
    "missing_summary",
    "missing_manifest",
    "manifest_write_mismatch",
    "forbidden_write",
]


@dataclass
class AdapterInvocation:
    target: str
    persona: str | None
    workspace_root: Path
    attempt: int
    node_id: str
    change_id: str


@dataclass
class FourLayerDeterministicAdapter:
    """Writes strict agent outputs keyed by exact target and attempt number."""

    mutations: dict[str, MutationName] = field(default_factory=dict)
    block_targets: set[str] = field(default_factory=set)
    allow_plan_skills: bool = False
    review_scripts: dict[str, Sequence[ReviewScript]] = field(default_factory=dict)
    invocations: list[AdapterInvocation] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)
    _block_events: dict[str, threading.Event] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def release(self, target: str) -> None:
        event = self._block_events.get(target)
        if event is not None:
            event.set()

    def hold(self, target: str) -> None:
        with self._lock:
            self.block_targets.add(target)
            self._block_events[target] = threading.Event()

    def invoke(self, request: AgentRequest) -> AgentResult:
        with self._lock:
            attempt = self._counts.get(request.target, 0) + 1
            self._counts[request.target] = attempt
            self.invocations.append(
                AdapterInvocation(
                    target=request.target,
                    persona=request.agent,
                    workspace_root=Path(request.workspace_root),
                    attempt=attempt,
                    node_id=request.node_id,
                    change_id=request.change_id,
                )
            )
            block_event = (
                self._block_events.get(request.target) if request.target in self.block_targets else None
            )

        if request.target in _PLAN_SKILLS and not self.allow_plan_skills:
            return AgentResult(
                ok=False,
                error_kind="internal",
                error=f"unexpected plan skill in codegen-only: {request.target}",
            )
        if block_event is not None:
            if not block_event.wait(timeout=30.0):
                return AgentResult(ok=False, error_kind="timeout", error=f"blocked on {request.target}")

        if request.target == "skill:aa-fix-proposal":
            change_root, _repo = _logical_roots(Path(request.workspace_root), request.change_id)
            return self._write_fix_proposal(change_root)

        layer = _LAYER_BY_TARGET.get(request.target)
        if layer is None:
            return AgentResult(
                ok=False,
                error_kind="internal",
                error=f"unexpected agent target: {request.target}",
            )
        mutation = self.mutations.get(request.target, "none")
        change_root, repo_root = _logical_roots(Path(request.workspace_root), request.change_id)
        if request.target in _REVIEWER_BY_LAYER.values():
            return self._write_review(
                layer,
                change_root,
                repo_root=repo_root,
                change_id=request.change_id,
                mutation=mutation,
                attempt=attempt,
            )
        if request.target in _PLAN_FIXER_BY_LAYER.values():
            return self._write_plan_fixer(layer, change_root)
        if request.target in _CODEGEN_FIXER_BY_LAYER.values():
            return self._write_codegen_fixer(layer, change_root, repo_root)
        return self._write_codegen(
            layer,
            change_root,
            repo_root,
            change_id=request.change_id,
            mutation=mutation,
        )

    def _write_review(
        self,
        layer: str,
        change_root: Path,
        *,
        repo_root: Path,
        change_id: str,
        mutation: MutationName,
        attempt: int,
    ) -> AgentResult:
        if mutation == "missing_review":
            return AgentResult(ok=True)
        bundle = load_canonical_assurance_bundle(layer)
        profile = get_layer_assurance_profile(layer)
        payload = json.loads(bundle.review_bytes.decode("utf-8"))
        payload["change_id"] = change_id
        scripts = list(self.review_scripts.get(layer, ()))
        script: ReviewScript = scripts[attempt - 1] if attempt <= len(scripts) else "pass"
        if script == "needs_fix_auto":
            payload["decision"] = "needs_fix"
            payload["auto_fix_allowed"] = True
            payload["next_action"] = "auto_fix"
            payload["auto_fix_plan"] = [
                {"path": profile.plan_artifacts[0], "action": "revise", "note": "deterministic fix"}
            ]
            payload["codegen_readiness"] = "not_ready"
        elif script in {"needs_fix_human", "needs_fix_human_missing_capability"}:
            payload["decision"] = "needs_fix"
            payload["auto_fix_allowed"] = False
            payload["human_review_required"] = True
            payload["next_action"] = "human_review"
            payload["codegen_readiness"] = "not_ready"
            if script == "needs_fix_human_missing_capability":
                payload["required_capabilities"] = [
                    "capabilities.domain_factories.account.missing_fixture_only"
                ]
        elif script == "knowledge_gap":
            payload["decision"] = "pass"
            # Must be a fully-qualified L1 leaf key (PlanReview validation) that is
            # absent from the seeded data-knowledge.yaml so the gate routes to
            # knowledge_remediation rather than pass.
            payload["required_capabilities"] = ["capabilities.domain_factories.account.missing_fixture_only"]
            payload["codegen_readiness"] = "ready"
        else:
            payload["decision"] = "pass"
            payload["auto_fix_allowed"] = False
            payload["codegen_readiness"] = "ready"
            available = _available_capability_keys(repo_root, change_root)
            requested = [
                str(item) for item in payload.get("required_capabilities") or [] if isinstance(item, str)
            ]
            kept = [item for item in requested if item in available]
            if not kept and available:
                kept = [sorted(available)[0]]
            payload["required_capabilities"] = kept
        review_path = change_root / profile.review_artifact
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        summary_rel = _REVIEW_SUMMARY_BY_LAYER[layer]
        summary_path = change_root / summary_rel
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(f"# {layer} plan review summary\n", encoding="utf-8")
        return AgentResult(ok=True)

    def _write_plan_fixer(self, layer: str, change_root: Path) -> AgentResult:
        profile = get_layer_assurance_profile(layer)
        plan_rel = profile.plan_artifacts[0]
        plan_path = change_root / plan_rel
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        existing = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else "# plan\n"
        if "deterministic-plan-fix" not in existing:
            plan_path.write_text(
                existing.rstrip() + "\n\n<!-- deterministic-plan-fix -->\n", encoding="utf-8"
            )
        summary_name = {
            "api": "review/api-plan-review-apply-summary.md",
            "e2e": "review/plan-review-apply-summary.md",
        }[layer]
        summary = change_root / summary_name
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(f"# {layer} plan fixer apply summary\n", encoding="utf-8")
        return AgentResult(ok=True)

    def _write_codegen_fixer(self, layer: str, change_root: Path, repo_root: Path) -> AgentResult:
        case_id, test_rel, _symbol = _CODEGEN_TEST_BY_LAYER[layer]
        test_path = repo_root / test_rel
        test_path.parent.mkdir(parents=True, exist_ok=True)
        if not test_path.is_file():
            test_path.write_text(_behavior_source(layer, case_id), encoding="utf-8")
        else:
            text = test_path.read_text(encoding="utf-8")
            if "deterministic-codegen-fix" not in text:
                test_path.write_text(text.rstrip() + "\n# deterministic-codegen-fix\n", encoding="utf-8")
        intent = {
            "schema_version": "1",
            "target": layer,
            "outcome": "applied",
            "proposal_ids": [f"{layer}-proposal-1"],
            "reason": "deterministic fixer",
            "claimed_modified_paths": [test_rel],
        }
        intent_path = change_root / "healing" / f"{layer}-apply-intent.json"
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(json.dumps(intent, indent=2) + "\n", encoding="utf-8")
        return AgentResult(ok=True)

    def _write_fix_proposal(self, change_root: Path) -> AgentResult:
        proposal = {
            "schema_version": "1",
            "proposals": [
                {
                    "proposal_id": "api-proposal-1",
                    "target": "api",
                    "eligible": True,
                    "risk_level": "low",
                    "paths": ["tests/api/test_accounts_api.py"],
                }
            ],
        }
        path = change_root / "healing" / "fix-proposal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
        return AgentResult(ok=True)

    def _write_codegen(
        self,
        layer: str,
        change_root: Path,
        repo_root: Path,
        *,
        change_id: str,
        mutation: MutationName,
    ) -> AgentResult:
        gf = get_generated_files_contract(layer)
        summary_rel = gf.summary_path.removeprefix("change:")
        manifest_rel = gf.manifest_path.removeprefix("change:")
        case_ids, test_rel, symbols = _resolve_codegen_targets(layer, change_root)
        chunks = [
            _behavior_source(layer, case_id, symbol=symbol)
            for case_id, symbol in zip(case_ids, symbols, strict=True)
        ]
        test_source = "\n".join(chunks)
        test_bytes = test_source.encode("utf-8")
        digest = sha256_bytes(test_bytes)

        if mutation != "missing_summary":
            summary_path = change_root / summary_rel
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(f"# {layer} codegen summary\n", encoding="utf-8")

        if mutation != "missing_manifest":
            manifest = _build_layer_manifest(
                layer,
                change_id=change_id,
                case_ids=case_ids,
                repo_path=test_rel,
                digest=digest,
            )
            if mutation == "manifest_write_mismatch":
                # Claim a path that will not be written.
                payload = manifest.model_dump(mode="json")
                payload["files"][0]["repo_path"] = f"{gf.private_test_root}/missing_test.py"
                manifest = get_generated_files_model(layer).model_validate(payload)
            manifest_path = change_root / manifest_rel
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(canonical_json_bytes(manifest))

        if mutation != "manifest_write_mismatch":
            test_path = repo_root / test_rel
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_bytes(test_bytes)

        if mutation == "forbidden_write":
            evil = repo_root / "app" / "evil.py"
            evil.parent.mkdir(parents=True, exist_ok=True)
            evil.write_text("print('forbidden')\n", encoding="utf-8")

        return AgentResult(ok=True)


@dataclass
class CoordinatorFaultInjector:
    """Coordinator-only snapshot/start-reference corruption (not an agent seam)."""

    stale_snapshot: bool = False
    corrupt_start_reference: bool = False
    applied: list[str] = field(default_factory=list)

    def maybe_corrupt_change(self, change_dir: Path) -> None:
        if self.stale_snapshot:
            snap = change_dir / ".graph-runtime" / "tree.json"
            if snap.is_file():
                snap.write_text('{"corrupted": true}\n', encoding="utf-8")
                self.applied.append("stale_snapshot")
        if self.corrupt_start_reference:
            inv = change_dir / ".graph-runtime" / "invocations"
            if inv.is_dir():
                for path in inv.glob("*.json"):
                    path.write_text("{}\n", encoding="utf-8")
                    self.applied.append(f"corrupt_start_reference:{path.name}")


@dataclass
class BarrierNodeRunner:
    """Test-only runner that can hold a node result before scheduler commit."""

    inner: NodeRunner | None = None
    hold_node_id: str | None = None
    hold_structural_suffix: str | None = None
    _gate: threading.Event = field(default_factory=threading.Event)
    held_tasks: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._gate.set()

    def arm(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        if self.inner is None:
            raise RuntimeError("BarrierNodeRunner.inner is not bound")
        result = self.inner.execute(task, workspace, context)
        if self.hold_node_id is not None and task.node_id == self.hold_node_id:
            if self.hold_structural_suffix is None or task.structural_path.endswith(
                self.hold_structural_suffix
            ):
                self.held_tasks.append(task.task_id)
                if not self._gate.wait(timeout=30.0):
                    return TaskResult(
                        status="failed",
                        error_kind="timeout",
                        error=f"barrier timeout on {task.node_id}",
                    )
        return result


@dataclass(frozen=True, slots=True)
class FourLayerRuntimeFixture:
    project_root: Path
    change_dir: Path
    change_id: str
    import_manifest_path: Path
    selected_layers: tuple[LayerName, ...]
    adapter: FourLayerDeterministicAdapter
    bundle: RuntimeBundle
    coordinator: CoordinatorFaultInjector


def seed_four_layer_project(
    tmp_path: Path,
    *,
    selected_layers: Sequence[LayerName],
    applicable_layers: Sequence[LayerName] | None = None,
    inapplicable_layers: Sequence[LayerName] = (),
    applicability_error_layer: LayerName | None = None,
    seed_stale_artifacts: bool = False,
) -> tuple[Path, Path, Path]:
    """Materialize a packaged-runtime project and plan-ready import manifest.

    Returns ``(project_root, change_dir, import_manifest_path)``.
    """
    selected: tuple[LayerName, ...] = tuple(layer for layer in LAYERS if layer in set(selected_layers))
    if not selected:
        raise ValueError("selected_layers must be non-empty")
    applicable: tuple[LayerName, ...] = (
        tuple(layer for layer in selected if layer in set(applicable_layers))
        if applicable_layers is not None
        else tuple(layer for layer in selected if layer not in set(inapplicable_layers))
    )

    project = tmp_path / "sut"
    change_dir = project / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    write_aa_config(project)
    _git_init(project)

    # Merge canonical layer fixtures into one change tree.
    merged_knowledge: dict[str, object] = {"version": 1, "auth": {}, "capabilities": {}}
    for layer in selected:
        bundle = load_canonical_assurance_bundle(layer)
        for rel, text in bundle.plan_texts.items():
            target = change_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for case_path in sorted((bundle.root / "cases").glob("**/case.yaml")):
            rel = case_path.relative_to(bundle.root)
            dest = change_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if layer in applicable and layer != applicability_error_layer:
                shutil.copy2(case_path, dest)
            elif layer == applicability_error_layer:
                dest.write_text("not: valid: yaml: [\n", encoding="utf-8")
            else:
                # Dynamic N/A: valid cases of another type / no selected automation.
                dest.write_text(
                    yaml.safe_dump(
                        {
                            "schema_version": "1.0",
                            "added": [
                                {
                                    "case_id": f"OTHER-{layer.upper()}-001",
                                    "title": "other layer only",
                                    "type": "API" if layer != "api" else "E2E",
                                    "automation": {"required": False},
                                }
                            ],
                            "modified": [],
                            "removed": [],
                        }
                    ),
                    encoding="utf-8",
                )
        for rel in bundle.adapter_paths:
            src = bundle.root / rel
            dest = project / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        knowledge = yaml.safe_load((bundle.root / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))
        _deep_merge(merged_knowledge, knowledge)

    # Keep a schema-valid project config (fixture configs are observation-only).
    write_aa_config(project)
    (project / ".aa" / "data-knowledge.yaml").write_text(
        yaml.safe_dump(merged_knowledge, sort_keys=False), encoding="utf-8"
    )
    (project / ".aa" / "policy.yaml").write_text(_policy_text(), encoding="utf-8")
    (change_dir / ".qa.yaml").write_text(
        yaml.safe_dump({"test_types": list(selected), "approval": {"mode": "autonomous"}}),
        encoding="utf-8",
    )
    (change_dir / "proposal.md").write_text("# proposal\n", encoding="utf-8")
    (change_dir / "workflow-state.yaml").write_text(
        "phases:\n  skill_registry_check: {status: pass}\n"
        "run_context: {interaction_mode: autonomous, orchestrator_skill: aa-workflow}\n",
        encoding="utf-8",
    )
    facts = change_dir / "facts" / "fact-baseline.json"
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(
        json.dumps(
            {
                "change_id": CHANGE_ID,
                "generated_at": "2026-08-01T00:00:00Z",
                "source": "four-layer-runtime",
                "facts": {"route_prefix": "/api/v1"},
                "warnings": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    registry = change_dir / "registry" / "skill-registry-check.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "pass",
                "healing_available": True,
                "reason": "fixture: healing skills registered",
                "required_skills": [
                    "aa-fix-proposal",
                    "aa-api-codegen-fixer",
                    "aa-e2e-codegen-fixer",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if seed_stale_artifacts:
        _seed_stale_pass_shaped(change_dir, project, selected)

    digest = hashlib.sha256(b"four-layer-runtime-v1").hexdigest()
    lock_dir = project / "eval-fixtures"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "fixture-lock.json").write_text(
        json.dumps({"fixtures": {_FIXTURE_ID: {"aggregate_sha256": digest}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    import_path = _write_bootstrap_import_manifest(change_dir, project, digest=digest)
    return project, change_dir, import_path


def build_four_layer_runtime(
    project_root: Path,
    *,
    adapter: FourLayerDeterministicAdapter,
    barrier: BarrierNodeRunner | None = None,
    clock: object | None = None,
) -> RuntimeBundle:
    bundle = build_graph_runtime(
        project_root=project_root,
        change_id=CHANGE_ID,
        adapter=adapter,
        clock=clock,  # type: ignore[arg-type]
    )
    if barrier is not None and bundle.resolved is not None:
        barrier.inner = bundle.resolved.node_runner
        bundle.resolved.scheduler._runner = barrier  # noqa: SLF001 — test-only seam
    return bundle


def run_codegen_only(
    fixture: FourLayerRuntimeFixture,
    *,
    selected_layers: Sequence[LayerName] | None = None,
    max_healing_attempts: int = 0,
) -> ImportResult:
    layers = tuple(selected_layers) if selected_layers is not None else fixture.selected_layers
    params: dict[str, object] = {
        "run_mode": "codegen-only",
        "test_types": list(layers),
        "run_tests": False,
        "max_healing_attempts": max_healing_attempts,
        "auto_archive": False,
    }
    context = runtime_context_for(fixture.project_root, fixture.change_id, params)
    manifest = parse_import_manifest(fixture.import_manifest_path.read_text(encoding="utf-8"))
    return fixture.bundle.runtime.import_checkpoint(fixture.bundle.compiled, manifest, context)


class InjectedCrash(BaseException):
    """Test-only crash cut.

    Must subclass ``BaseException`` so subgraph/task runners that catch
    ``Exception`` cannot convert the cut into a failed TaskResult.
    """


@dataclass
class CrashAfterCommittedNode:
    """Raise after the Nth commit of ``node_id`` so tests can rebuild and resume."""

    node_id: str
    occurrence: int = 1
    structural_suffix: str | None = None
    hits: int = 0
    _orig: object | None = field(default=None, repr=False)

    def install(self, scheduler: object) -> None:
        from assurance_agent.workflow.graph.checkpoint import project_invocation

        commit_wave = scheduler._commit_wave  # type: ignore[attr-defined]  # noqa: SLF001
        self._orig = commit_wave

        def wrapped(**kwargs):  # type: ignore[no-untyped-def]
            projection = kwargs["projection"]
            context = kwargs["context"]
            live_before = project_invocation(context.change_dir, projection.invocation_id)
            pending = [
                task_id
                for task_id, task in live_before.tasks.items()
                if task.status == "succeeded"
                and not task.outputs_committed
                and task.node_id == self.node_id
                and (
                    self.structural_suffix is None
                    or self.structural_suffix in task_id
                    or self.structural_suffix in (getattr(task, "task_key", None) or "")
                )
            ]
            result = commit_wave(**kwargs)
            if not pending:
                return result
            live_after = project_invocation(context.change_dir, projection.invocation_id)
            for task_id in pending:
                after = live_after.tasks.get(task_id)
                if after is None or not after.outputs_committed:
                    continue
                self.hits += 1
                if self.hits == self.occurrence:
                    raise InjectedCrash(f"crash_after_commit:{self.node_id}:{self.occurrence}")
            return result

        scheduler._commit_wave = wrapped  # type: ignore[attr-defined]  # noqa: SLF001

    def uninstall(self, scheduler: object) -> None:
        if self._orig is not None:
            scheduler._commit_wave = self._orig  # type: ignore[attr-defined]  # noqa: SLF001


def prepare_inprocess_crash_resume(fixture: FourLayerRuntimeFixture) -> None:
    """Expire live leases after an in-process InjectedCrash.

    InjectedCrash keeps the test PID alive, so lease liveness probes would
    otherwise wait forever. Clear lease files so recovery falls back to ledger
    expiry, which the resumed FakeClock advances past. Also drop empty/stale
    project lock files left by the aborted process so synchronized replan can
    reserve again.
    """
    leases = fixture.change_dir / ".graph-runtime" / "leases"
    if leases.is_dir():
        for path in leases.glob("*"):
            if path.is_file():
                path.unlink()
    running = fixture.change_dir / ".graph-runtime" / "running-tasks.json"
    if running.is_file():
        running.write_text('{"schema_version":1,"leases":[]}\n', encoding="utf-8")
    project_locks = fixture.project_root / "qa" / ".graph-runtime" / "locks"
    if project_locks.is_dir():
        for path in project_locks.glob("*"):
            if path.is_file():
                path.unlink()


def rebuild_runtime(
    fixture: FourLayerRuntimeFixture,
    *,
    barrier: BarrierNodeRunner | None = None,
    far_future_clock: bool = True,
) -> FourLayerRuntimeFixture:
    """Construct a fresh GraphRuntime against the same durable change ledger."""
    clock = None
    if far_future_clock:
        from datetime import datetime, timedelta, timezone

        from tests.integration._graph_fault_worker import FakeClock

        clock = FakeClock(start=datetime(2099, 1, 1, tzinfo=timezone.utc))
        # Allow heartbeat math without sleeping the suite.
        clock.sleep(0)
        _ = timedelta
    prepare_inprocess_crash_resume(fixture)
    bundle = build_four_layer_runtime(
        fixture.project_root,
        adapter=fixture.adapter,
        barrier=barrier,
        clock=clock,
    )
    return FourLayerRuntimeFixture(
        project_root=fixture.project_root,
        change_dir=fixture.change_dir,
        change_id=fixture.change_id,
        import_manifest_path=fixture.import_manifest_path,
        selected_layers=fixture.selected_layers,
        adapter=fixture.adapter,
        bundle=bundle,
        coordinator=fixture.coordinator,
    )


def resume_root(fixture: FourLayerRuntimeFixture, invocation_id: str, command=None):  # noqa: ANN001
    from assurance_agent.workflow.graph.models import ResumeCommand

    if command is not None and not isinstance(command, ResumeCommand):
        raise TypeError("command must be ResumeCommand | None")
    return fixture.bundle.runtime.resume(invocation_id, command)


def make_fixture(
    tmp_path: Path,
    *,
    selected_layers: Sequence[LayerName],
    applicable_layers: Sequence[LayerName] | None = None,
    inapplicable_layers: Sequence[LayerName] = (),
    applicability_error_layer: LayerName | None = None,
    seed_stale_artifacts: bool = False,
    mutations: Mapping[str, MutationName] | None = None,
    review_scripts: Mapping[str, Sequence[ReviewScript]] | None = None,
    barrier: BarrierNodeRunner | None = None,
    coordinator: CoordinatorFaultInjector | None = None,
) -> FourLayerRuntimeFixture:
    project, change_dir, import_path = seed_four_layer_project(
        tmp_path,
        selected_layers=selected_layers,
        applicable_layers=applicable_layers,
        inapplicable_layers=inapplicable_layers,
        applicability_error_layer=applicability_error_layer,
        seed_stale_artifacts=seed_stale_artifacts,
    )
    adapter = FourLayerDeterministicAdapter(
        mutations=dict(mutations or {}),
        review_scripts={str(k): tuple(v) for k, v in (review_scripts or {}).items()},
    )
    fault = coordinator or CoordinatorFaultInjector()
    bundle = build_four_layer_runtime(project, adapter=adapter, barrier=barrier)
    return FourLayerRuntimeFixture(
        project_root=project,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        import_manifest_path=import_path,
        selected_layers=tuple(layer for layer in LAYERS if layer in set(selected_layers)),
        adapter=adapter,
        bundle=bundle,
        coordinator=fault,
    )


def root_events(change_dir: Path) -> list[dict[str, object]]:
    return list(read_events_strict(change_dir))


def events_for_root(
    events: Sequence[Mapping[str, object]], root_invocation_id: str
) -> list[dict[str, object]]:
    """Return events belonging to ``root_invocation_id`` or any nested child thereof."""
    allowed = {root_invocation_id}
    out: list[dict[str, object]] = []
    for event in events:
        inv = event.get("invocation_id")
        parent = event.get("parent_invocation_id")
        if isinstance(parent, str) and parent in allowed and isinstance(inv, str):
            allowed.add(inv)
        if isinstance(inv, str) and inv in allowed:
            out.append(dict(event))
    return out


def attempt_nodes(
    events: Sequence[Mapping[str, object]], *, event_type: str = "task_attempt_started"
) -> list[str]:
    return [str(e["node_id"]) for e in events if e.get("type") == event_type and e.get("node_id")]


def count_attempts(
    events: Sequence[Mapping[str, object]],
    *,
    node_id: str,
    event_type: str = "task_attempt_started",
) -> int:
    return sum(1 for e in events if e.get("type") == event_type and e.get("node_id") == node_id)


def assert_no_plan_or_run_tests(events: Sequence[Mapping[str, object]]) -> None:
    started = {(e.get("node_id"), e.get("target")) for e in events if e.get("type") == "task_attempt_started"}
    for node_id, target in started:
        assert node_id != "plan", "codegen-only must not start plan"
        assert target not in _PLAN_SKILLS, f"codegen-only must not invoke {target}"
        assert target != "operation:run-tests", "codegen-only run_tests=false must skip run-tests"


def _safe_tree_prefix(prefix: str) -> bool:
    if prefix in {".", ""}:
        return True
    parts = PurePosixPath(prefix).parts
    return bool(parts) and not any(part in {"", ".", ".."} for part in parts)


def _logical_roots(workspace_root: Path, change_id: str) -> tuple[Path, Path]:
    """Resolve change/repo roots inside a materialized task workspace.

    Paths stay under ``workspace_root`` (no ``resolve()``) so macOS
    ``/var`` vs ``/private/var`` symlink normalization cannot escape the task root.
    """
    manifest_candidates = (
        workspace_root / ".graph-runtime" / "tree.json",
        workspace_root.parent.parent / "task-sidecars" / workspace_root.name / "tree.json",
    )
    for manifest_path in manifest_candidates:
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        roots = payload.get("roots") if isinstance(payload, dict) else None
        if not isinstance(roots, dict):
            continue
        change_prefix = roots.get("change")
        repo_prefix = roots.get("repo")
        if not isinstance(change_prefix, str) or not isinstance(repo_prefix, str):
            continue
        if not _safe_tree_prefix(change_prefix) or not _safe_tree_prefix(repo_prefix):
            continue
        change_root = workspace_root if change_prefix in {".", ""} else workspace_root / change_prefix
        repo_root = workspace_root if repo_prefix in {".", ""} else workspace_root / repo_prefix
        if change_root.is_dir() and repo_root.is_dir():
            return change_root, repo_root
    nested = workspace_root / "qa" / "changes" / change_id
    if nested.is_dir():
        return nested, workspace_root
    return workspace_root, workspace_root


def _behavior_source(layer: str, case_id: str, *, symbol: str | None = None) -> str:
    if layer == "api":
        fn = symbol or "test_api_acc_001__create_account_success"
        return (
            f"def {fn}(client):\n"
            f"    response = client.post('/api/v1/api/create', json={{'name': '{case_id}'}})\n"
            f"    assert response.status_code == 201\n"
            f"    assert 'id' in response.json()\n"
        )
    if layer == "e2e":
        fn = symbol or "test_tc_e2e_auth_reject__limited_user_denied"
        return (
            "from playwright.sync_api import expect\n\n"
            f"def {fn}(page):\n"
            "    page.goto('/')\n"
            "    page.click('button')\n"
            "    expect(page.locator('text=denied')).to_be_visible()\n"
        )
    if layer == "fuzz":
        fn = symbol or "test_fuzz_001__account_create_schema"
        return (
            "import schemathesis\n\n"
            "schema = schemathesis.openapi.from_asgi('/openapi.json', app=None)\n\n"
            "@schema.parametrize()\n"
            f"def {fn}(case):\n"
            "    case.call_and_validate()\n"
        )
    method = symbol or "get_accounts"
    return (
        "from locust import HttpUser, task\n\n"
        "class ApiUser(HttpUser):\n"
        "    @task\n"
        f"    def {method}(self):\n"
        "        self.client.get('/api/v1/api/list')\n"
    )


def _resolve_codegen_targets(layer: str, change_root: Path) -> tuple[list[str], str, list[str]]:
    """Prefer mapped targets from the workspace plan; fall back to helper defaults."""
    default_case, default_path, default_symbol = _CODEGEN_TEST_BY_LAYER[layer]
    profile = get_layer_assurance_profile(layer)
    plan_rel = None
    for rel in profile.plan_artifacts:
        if "codegen" in Path(rel).name:
            plan_rel = rel
            break
    if plan_rel is None and profile.plan_artifacts:
        plan_rel = profile.plan_artifacts[0]
    if plan_rel is None:
        return [default_case], default_path, [default_symbol]
    plan_path = change_root / plan_rel
    if not plan_path.is_file():
        return [default_case], default_path, [default_symbol]
    try:
        from assurance_agent.verification.generated_entries import extract_layer_mapping

        cases: list[dict[str, object]] = []
        cases_root = change_root / "cases"
        if cases_root.is_dir():
            for case_file in sorted(cases_root.rglob("case.yaml")):
                payload = yaml.safe_load(case_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    cases.append(payload)
        relation = extract_layer_mapping(
            layer=layer,
            plan_text=plan_path.read_text(encoding="utf-8"),
            cases=cases,
        )
        if relation.entries:
            # Emit one file covering every mapped case that shares the first target.
            target = relation.entries[0].target_file
            selected = [entry for entry in relation.entries if entry.target_file == target]
            return (
                [entry.case_id for entry in selected],
                target,
                [entry.symbol for entry in selected],
            )
    except Exception:
        return [default_case], default_path, [default_symbol]
    return [default_case], default_path, [default_symbol]


def _available_capability_keys(*roots: Path) -> set[str]:
    keys: set[str] = set()
    for root in roots:
        path = root / ".aa" / "data-knowledge.yaml"
        if not path.is_file():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        caps = payload.get("capabilities")
        if not isinstance(caps, dict):
            continue
        _walk_capability_keys(caps, "capabilities", keys)
        auth = payload.get("auth")
        if isinstance(auth, dict):
            for name in auth:
                if isinstance(name, str):
                    keys.add(f"auth.{name}")
    return keys


def _walk_capability_keys(node: object, prefix: str, out: set[str]) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if not isinstance(key, str):
            continue
        path = f"{prefix}.{key}"
        if isinstance(value, dict) and ("kind" in value or "symbol" in value):
            out.add(path)
            continue
        _walk_capability_keys(value, path, out)


def _build_layer_manifest(
    layer: str,
    *,
    change_id: str,
    case_id: str | None = None,
    case_ids: Sequence[str] | None = None,
    repo_path: str,
    digest: str,
) -> GeneratedFilesV1:
    model = get_generated_files_model(layer)
    ids = list(case_ids) if case_ids is not None else [case_id or "CASE-001"]
    return model.model_validate(
        {
            "schema_version": "1",
            "change_id": change_id,
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ids,
                    "content_sha256": digest if digest.startswith("sha256:") else f"sha256:{digest}",
                }
            ],
        }
    )


def _seed_stale_pass_shaped(
    change_dir: Path,
    project: Path,
    selected: Sequence[LayerName],
) -> None:
    for layer in selected:
        bundle = load_canonical_assurance_bundle(layer)
        profile = get_layer_assurance_profile(layer)
        review = change_dir / profile.review_artifact
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_bytes(bundle.review_bytes)
        checks = change_dir / profile.checks_artifact
        checks.parent.mkdir(parents=True, exist_ok=True)
        checks.write_text(
            json.dumps(
                {
                    "schema_version": "2",
                    "layer": layer,
                    "change_id": CHANGE_ID,
                    "checks": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        gf = get_generated_files_contract(layer)
        summary = change_dir / gf.summary_path.removeprefix("change:")
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("# stale summary\n", encoding="utf-8")
        manifest = change_dir / gf.manifest_path.removeprefix("change:")
        case_id, test_rel, _ = _CODEGEN_TEST_BY_LAYER[layer]
        stale_manifest = _build_layer_manifest(
            layer,
            change_id=CHANGE_ID,
            case_id=case_id,
            repo_path=test_rel,
            digest=sha256_bytes(b"stale\n"),
        )
        manifest.write_bytes(canonical_json_bytes(stale_manifest))
        # Do not seed the private test path: disposition=generated requires add.
        # Stale authority is proven via review/checks/summary/manifest bytes alone.
        _ = (project, test_rel)


def _write_bootstrap_import_manifest(change_dir: Path, project: Path, *, digest: str) -> Path:
    from assurance_agent.workflow.graph.compiler import compile_packaged_workflow
    from assurance_agent.workflow.graph.contracts import load_execution_contracts
    from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin
    from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view

    def _hash(logical: str) -> str:
        root, _, rest = logical.partition(":")
        base = {"change": change_dir, "project": project, "repo": project}[root]
        return "sha256:" + hashlib.sha256((base / rest).read_bytes()).hexdigest()

    registry_hash = _hash("change:registry/skill-registry-check.json")
    facts_hash = _hash("change:facts/fact-baseline.json")
    loaded = load_workflow_v2_with_origin(project)
    contracts = load_execution_contracts(project)
    compiled = compile_packaged_workflow(loaded.schema, contracts)
    gate_ctx = GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={"run_mode": "codegen-only", "test_types": ["api"], "run_tests": False},
        state_values={},
        node_results={},
    )
    report = check_gate_in_view(compiled.schema.gates, "registry-gate", gate_ctx)
    payload = {
        "schema_version": "2",
        "entrypoint": "execute",
        "source": {
            "kind": "eval-fixture",
            "fixture_id": _FIXTURE_ID,
            "fixture_digest": f"sha256:{digest}",
        },
        "inputs": {
            "change:proposal.md": _hash("change:proposal.md"),
            "change:.qa.yaml": _hash("change:.qa.yaml"),
            "change:facts/fact-baseline.json": facts_hash,
        },
        "completed": [
            {
                "path": "execute-workflow/bootstrap/bootstrap",
                "graph": "bootstrap",
                "node": "registry",
                "outputs": {"change:registry/skill-registry-check.json": registry_hash},
                "gate": {
                    "id": "registry-gate",
                    "verdict": report.verdict.value,
                    "reads_sha256": dict(report.reads_sha256),
                },
            },
            {
                "path": "execute-workflow/assurance/assurance",
                "graph": "assurance",
                "node": "fact-baseline",
                "outputs": {"change:facts/fact-baseline.json": facts_hash},
            },
        ],
        "budgets": [],
    }
    out = change_dir / ".graph-runtime" / "import-manifest.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return out


def _policy_text() -> str:
    lines = [
        "version: 1",
        "human_review_risk_levels: [high, critical]",
        "force_continue_allowed: true",
        "plan_checks:",
        "  l1_path: warn",
        "  shared_factory: warn",
        "  assert_ideal: warn",
        "  capability_keys: warn",
        "coverage_floor:",
        "  risk_high: 0.9",
        "  risk_medium: 0.7",
        "fuzz:",
        "  required_when_endpoint_has_auth: true",
        "healing:",
        "  auth_module: require_human",
        "",
    ]
    return "\n".join(lines)


def _deep_merge(dst: dict[str, object], src: Mapping[str, object]) -> None:
    for key, value in src.items():
        if key == "version":
            dst[key] = value
            continue
        existing = dst.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)  # type: ignore[arg-type]
        else:
            dst[key] = value  # type: ignore[assignment]


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


__all__ = [
    "CHANGE_ID",
    "NODE_CHAIN_AFTER",
    "AdapterInvocation",
    "BarrierNodeRunner",
    "CoordinatorFaultInjector",
    "CrashAfterCommittedNode",
    "InjectedCrash",
    "FourLayerDeterministicAdapter",
    "FourLayerRuntimeFixture",
    "LAYERS",
    "ReviewScript",
    "assert_no_plan_or_run_tests",
    "attempt_nodes",
    "build_four_layer_runtime",
    "count_attempts",
    "events_for_root",
    "make_fixture",
    "rebuild_runtime",
    "resume_root",
    "root_events",
    "run_codegen_only",
    "seed_four_layer_project",
]
