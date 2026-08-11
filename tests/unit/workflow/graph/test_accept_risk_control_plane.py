"""Hash-anchored ``accept_risk`` is a scoped control-plane fact.

The decision remains in the immutable graph ledger.  Gate evaluation and agent
prompts may consume it only when the interrupt/resume pair is on the current
checkpoint namespace ancestry and every audited artifact still has the bytes a
human accepted.
"""

from __future__ import annotations

import json
import textwrap
from hashlib import sha256
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import canonical_digest, compile_workflow
from assurance_agent.workflow.graph.contracts import ResourceClaims, parse_execution_contracts
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import (
    RetryPolicyDef,
    TimeoutPolicyDef,
    load_workflow_v2,
    parse_workflow_v2,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from tests.helpers_aa import write_aa_config


_ACCEPTANCE_HEADING = "VALID ANCESTOR ACCEPT_RISK DECISIONS"


def _compiled():
    return compile_workflow(
        parse_workflow_v2(
            textwrap.dedent(
                """
                schema_version: "2"
                name: accept-risk-control-plane
                entrypoints:
                  full: {graph: main}
                policies:
                  retry:
                    never: {max_attempts: 1}
                  timeout:
                    local: {run_seconds: 60, heartbeat_seconds: 10}
                    model: {run_seconds: 60, heartbeat_seconds: 10}
                graphs:
                  main:
                    max_supersteps: 5
                    nodes:
                      precheck:
                        uses: builtin:gate
                        with: {gate: referring-gate}
                        retry: never
                        timeout: local
                      author:
                        uses: skill:test-author
                        outputs: [change:codegen/out.md]
                        retry: never
                        timeout: model
                    edges:
                      - {from: START, to: precheck}
                      - {from: precheck, to: author}
                      - {from: author, to: END}
                gates:
                  leaf-gate:
                    reads:
                      - {path: review/leaf.json, as: leaf}
                    invalid_json: stop
                    missing_field_is: stop
                    missing_file_is: stop
                    needs_human_review_when: "leaf.decision == 'needs_human_review'"
                    reject_when: "leaf.decision == 'reject'"
                    pass_when: "leaf.decision == 'pass'"
                  other-gate:
                    reads:
                      - {path: review/leaf.json, as: leaf}
                    invalid_json: stop
                    missing_field_is: stop
                    missing_file_is: stop
                    needs_human_review_when: "leaf.decision == 'needs_human_review'"
                    pass_when: "leaf.decision == 'pass'"
                  referring-gate:
                    reads: []
                    stop_when: "gate('leaf-gate').verdict != 'pass'"
                    pass_when: "gate('leaf-gate').verdict == 'pass'"
                """
            )
        )
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    change_dir = project / "qa" / "changes" / "CH-1"
    (change_dir / "review").mkdir(parents=True)
    (change_dir / "review" / "leaf.json").write_text(
        json.dumps({"decision": "needs_human_review"}), encoding="utf-8"
    )
    write_aa_config(project)
    return project


def _context(project: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
    )


def _workspace(project: Path, task_id: str) -> TaskWorkspace:
    change_dir = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change_dir)
    tree_id = store.capture(project)
    return WorkspaceBackend(change_dir).create(task_id=task_id, base_tree_id=tree_id, store=store)


def _task(
    target: str,
    *,
    node_id: str,
    checkpoint_ns: str,
    task_id: str,
    with_: dict[str, object] | None = None,
) -> ExecutableTask:
    return ExecutableTask(
        task_id=task_id,
        invocation_id=checkpoint_ns.split("/")[-1],
        checkpoint_ns=checkpoint_ns,
        graph_id="main",
        node_id=node_id,
        structural_path="main",
        input={"with": with_ or {}, "context": {"change_id": "CH-1"}},
        input_sha256="input",
        contract_digest="contract",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=60.0, heartbeat_seconds=10.0),
        target=target,
        resources=ResourceClaims(),
    )


def _layer_namespaces(full_ns: str) -> list[tuple[str, str, str]]:
    """Return ``(invocation_id, layer_ns, node_id)`` for a v3 namespace."""
    parts = [part for part in full_ns.split("/") if part]
    layers: list[tuple[str, str, str]] = []
    for index in range(0, len(parts), 2):
        node_id = parts[index + 1] if index + 1 < len(parts) else "human-review"
        layers.append((parts[index], "/".join(parts[: index + 1]), node_id))
    return layers


def _append_acceptance(
    change_dir: Path,
    *,
    full_ns: str,
    checkpoint: str = "leaf-gate",
    action: str = "accept_risk",
    interrupt_id: str = "interrupt-1",
    audited_digest: str | None = None,
    audited_hashes: dict[str, str] | None = None,
    resume_layer_limit: int | None = None,
    corrupt_parent_anchor: bool = False,
    resume_before_interrupt: bool = False,
    reverse_resume_order: bool = False,
    resume_hash_layer: int = 0,
    corrupt_resume_anchor_node_layer: int | None = None,
) -> None:
    digest = audited_digest or canonical_digest({"decision": "needs_human_review"})
    audited = audited_hashes or {"review/leaf.json": digest}
    layers = _layer_namespaces(full_ns)
    interrupted_events: list[dict[str, object]] = []
    for invocation_id, _, node_id in layers:
        interrupted_events.append(
            {
                "source": "graph",
                "type": "graph_interrupted",
                "invocation_id": invocation_id,
                "checkpoint_ns": full_ns,
                "interrupt_id": interrupt_id,
                "node_id": node_id,
                "checkpoint": checkpoint,
                "actions": ["fix_and_proceed", "accept_risk", "stop"],
                "audited_reads_sha256": audited,
                "anchor": {
                    "invocation_id": invocation_id,
                    "checkpoint_ns": full_ns,
                    "node_id": node_id,
                    "interrupt_id": interrupt_id,
                },
            }
        )

    parent_anchor_ref: str | None = None
    resume_layers = layers if resume_layer_limit is None else layers[:resume_layer_limit]
    resumed_events: list[dict[str, object]] = []
    for index, (invocation_id, layer_ns, node_id) in enumerate(resume_layers):
        anchor = {
            "invocation_id": invocation_id,
            "checkpoint_ns": layer_ns,
            "node_id": ("wrong-node" if corrupt_resume_anchor_node_layer == index else node_id),
            "interrupt_id": interrupt_id,
        }
        event: dict[str, object] = {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": invocation_id,
            "checkpoint_ns": layer_ns,
            "interrupt_id": interrupt_id,
            "action": action,
            "reason": "reviewed and accepted for this evidence",
            "who": "reviewer",
            "audited_reads_sha256": audited if index == resume_hash_layer else {},
            "anchor": anchor,
            "payload": {},
        }
        if parent_anchor_ref is not None:
            event["parent_anchor_ref"] = (
                "corrupt-parent-anchor" if corrupt_parent_anchor and index == 1 else parent_anchor_ref
            )
        resumed_events.append(event)
        parent_anchor_ref = canonical_digest(anchor)

    if reverse_resume_order:
        resumed_events.reverse()
    ordered_events = (
        [*resumed_events, *interrupted_events]
        if resume_before_interrupt
        else [*interrupted_events, *resumed_events]
    )
    for event in ordered_events:
        append_event_strict(change_dir, event)


def _actual_review_digest(project: Path) -> str:
    path = project / "qa" / "changes" / "CH-1" / "review" / "leaf.json"
    return sha256(path.read_bytes()).hexdigest()


def _gate_value(project: Path, *, checkpoint_ns: str) -> str:
    task = _task(
        "builtin:gate",
        node_id="precheck",
        checkpoint_ns=checkpoint_ns,
        task_id="gate-task",
        with_={"gate": "referring-gate"},
    )
    result = GateHandler(_compiled()).execute(task, _workspace(project, task.task_id), _context(project))
    assert result.status == "succeeded"
    assert isinstance(result.value, str)
    return result.value


class _PromptRecorder:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        output = request.workspace_root / "qa" / "changes" / "CH-1" / "codegen" / "out.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("generated\n", encoding="utf-8")
        return AgentResult(ok=True)


def _agent_prompt(project: Path, *, checkpoint_ns: str) -> str:
    recorder = _PromptRecorder()
    contracts = parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:test-author:\n"
        "    handler: agent\n"
        "    writes: [change:codegen/**]\n"
        "    authorization_writes: [change:codegen/**]\n"
    )
    handler = AgentHandler(
        recorder,
        TreeStore(project / "qa" / "changes" / "CH-1"),
        contracts=contracts,
        compiled=_compiled(),
    )
    task = _task(
        "skill:test-author",
        node_id="author",
        checkpoint_ns=checkpoint_ns,
        task_id="author-task",
    )
    result = handler.execute(task, _workspace(project, task.task_id), _context(project))
    assert result.status == "succeeded", result.error
    return recorder.requests[0].prompt


def test_gate_does_not_reuse_an_acceptance_from_a_different_root_invocation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="old-root/review/old-review",
        audited_digest=_actual_review_digest(project),
    )

    assert _gate_value(project, checkpoint_ns="current-root") == "stop"


def test_later_unrelated_root_does_not_shadow_the_current_roots_acceptance(tmp_path: Path) -> None:
    project = _project(tmp_path)
    digest = _actual_review_digest(project)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="current-root",
        interrupt_id="current-interrupt",
        audited_digest=digest,
    )
    _append_acceptance(
        _context(project).change_dir,
        full_ns="other-root",
        interrupt_id="later-unrelated-interrupt",
        audited_digest=digest,
    )

    assert _gate_value(project, checkpoint_ns="current-root") == "pass"


@pytest.mark.parametrize(
    ("checkpoint", "action"),
    [
        ("other-gate", "accept_risk"),
        ("leaf-gate", "fix_and_proceed"),
        ("leaf-gate", "stop"),
    ],
)
def test_gate_ignores_the_wrong_checkpoint_or_action(
    tmp_path: Path,
    checkpoint: str,
    action: str,
) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root",
        checkpoint=checkpoint,
        action=action,
        audited_digest=_actual_review_digest(project),
    )

    assert _gate_value(project, checkpoint_ns="root") == "stop"


def test_gate_rejects_an_incomplete_nested_resume_chain(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        resume_layer_limit=1,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_gate_rejects_nested_namespace_with_repeated_invocation_id(tmp_path: Path) -> None:
    project = _project(tmp_path)
    change_dir = _context(project).change_dir
    full_ns = "same/review/same"
    digest = _actual_review_digest(project)
    anchor = {
        "invocation_id": "same",
        "checkpoint_ns": full_ns,
        "node_id": "human-review",
        "interrupt_id": "interrupt-1",
    }
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "same",
            "checkpoint_ns": full_ns,
            "interrupt_id": "interrupt-1",
            "node_id": "human-review",
            "checkpoint": "leaf-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": digest},
            "anchor": anchor,
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "same",
            "checkpoint_ns": full_ns,
            "interrupt_id": "interrupt-1",
            "action": "accept_risk",
            "reason": "must not collapse two namespace layers",
            "who": "reviewer",
            "audited_reads_sha256": {"review/leaf.json": digest},
            "anchor": anchor,
            "payload": {},
        },
    )

    assert _gate_value(project, checkpoint_ns=f"{full_ns}/codegen/codegen-inv") == "stop"


def test_gate_rejects_a_broken_nested_resume_anchor_chain(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        corrupt_parent_anchor=True,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_gate_rejects_resume_events_that_precede_their_interrupt(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root",
        audited_digest=_actual_review_digest(project),
        resume_before_interrupt=True,
    )

    assert _gate_value(project, checkpoint_ns="root") == "stop"


def test_gate_rejects_nested_resume_events_emitted_leaf_before_root(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        reverse_resume_order=True,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_gate_requires_the_root_resume_to_own_audited_hashes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        resume_hash_layer=1,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_gate_rejects_resume_anchor_with_wrong_structural_node(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        corrupt_resume_anchor_node_layer=0,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_gate_rejects_resume_anchor_with_wrong_leaf_node(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/review/review-inv",
        audited_digest=_actual_review_digest(project),
        corrupt_resume_anchor_node_layer=1,
    )

    assert _gate_value(project, checkpoint_ns="root/codegen/codegen-inv") == "stop"


def test_nested_ancestor_acceptance_is_projected_without_rewriting_findings_as_pass(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root/mid/mid-inv/review/review-inv",
        audited_digest=_actual_review_digest(project),
    )

    prompt = _agent_prompt(project, checkpoint_ns="root/mid/mid-inv/codegen/codegen-inv")

    assert _ACCEPTANCE_HEADING in prompt
    assert "leaf-gate" in prompt
    assert "interrupt-1" in prompt
    assert "allows continuation" in prompt
    assert "does not change any finding or review verdict to pass" in prompt


def test_gate_reference_applies_acceptance_to_a_frozen_attached_gate_report(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns="root",
        audited_digest=_actual_review_digest(project),
    )
    frozen_gate = {
        "gate_id": "leaf-gate",
        "verdict": "needs_human_review",
        "matched_rule": "needs_human_review_when",
    }
    context = GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=_context(project).change_dir,
        change_id="CH-1",
        params={},
        state_values={},
        node_results={"review": {"gate": frozen_gate}},
        audit_events_dir=_context(project).change_dir,
        checkpoint_ns="root",
    )

    report = check_gate_in_view(_compiled().schema.gates, "referring-gate", context)

    assert report.verdict.value == "pass"
    assert frozen_gate["verdict"] == "needs_human_review"


@pytest.mark.parametrize(
    ("decision_root", "checkpoint", "action", "drift"),
    [
        ("other-root", "leaf-gate", "accept_risk", False),
        ("root", "leaf-gate", "stop", False),
        ("root", "leaf-gate", "accept_risk", True),
    ],
)
def test_prompt_hides_unrelated_wrong_action_and_drifted_decisions(
    tmp_path: Path,
    decision_root: str,
    checkpoint: str,
    action: str,
    drift: bool,
) -> None:
    project = _project(tmp_path)
    _append_acceptance(
        _context(project).change_dir,
        full_ns=decision_root,
        checkpoint=checkpoint,
        action=action,
        audited_digest=_actual_review_digest(project),
    )
    if drift:
        review = project / "qa" / "changes" / "CH-1" / "review" / "leaf.json"
        review.write_text(json.dumps({"decision": "reject"}), encoding="utf-8")

    prompt = _agent_prompt(project, checkpoint_ns="root/codegen/codegen-inv")

    assert _ACCEPTANCE_HEADING not in prompt


def test_malformed_ledger_is_false_for_gate_and_prompt_without_mutating_history(tmp_path: Path) -> None:
    project = _project(tmp_path)
    change_dir = _context(project).change_dir
    _append_acceptance(
        change_dir,
        full_ns="root",
        audited_digest=_actual_review_digest(project),
    )
    events_path = change_dir / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write("{malformed-ledger\n")
    before = events_path.read_bytes()

    assert _gate_value(project, checkpoint_ns="root") == "stop"
    assert _ACCEPTANCE_HEADING not in _agent_prompt(project, checkpoint_ns="root")
    assert events_path.read_bytes() == before


@pytest.mark.parametrize(
    ("review_gate", "precondition_gate", "review_rel", "include_api_checks"),
    [
        ("api-plan-review-gate", "api-codegen-precondition-gate", "review/api-plan-review.json", True),
        ("e2e-plan-review-gate", "e2e-codegen-precondition-gate", "review/plan-review.json", False),
    ],
)
def test_packaged_codegen_precondition_accepts_exact_risk_without_dropping_hard_inputs(
    tmp_path: Path,
    review_gate: str,
    precondition_gate: str,
    review_rel: str,
    include_api_checks: bool,
) -> None:
    project = _project(tmp_path)
    change_dir = _context(project).change_dir
    data_knowledge = project / ".aa" / "data-knowledge.yaml"
    data_knowledge.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {},
                "auth": {},
                "entities": {},
                "capabilities": {
                    "domain_factories": {},
                    "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
                    "cleanup": {},
                },
            }
        ),
        encoding="utf-8",
    )
    review_path = change_dir / review_rel
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(
        json.dumps(
            {
                "decision": "needs_human_review",
                "codegen_readiness": "ready",
                "required_capabilities": ["auth.missing_for_review"],
                "auto_fix_allowed": False,
                "human_review_required": True,
                "risk_level": "high",
            }
        ),
        encoding="utf-8",
    )
    audited_paths = [review_rel, "repo:.aa/data-knowledge.yaml"]
    if include_api_checks:
        checks_rel = "review/api-plan-checks.json"
        checks_path = change_dir / checks_rel
        checks_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    # Overall failure is still compatible with per-check warn.
                    "status": "fail",
                    "checks": [
                        {
                            "check_id": "assert_ideal",
                            "status": "fail",
                            "findings": [{"locator": "x", "actual": "a", "expected": "b"}],
                            "refs": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        audited_paths.append(checks_rel)
    audited_hashes: dict[str, str] = {}
    for rel in audited_paths:
        path = data_knowledge if rel.startswith("repo:") else change_dir / rel
        audited_hashes[rel] = sha256(path.read_bytes()).hexdigest()
    _append_acceptance(
        change_dir,
        full_ns="root/review/review-inv",
        checkpoint=review_gate,
        audited_hashes=audited_hashes,
    )
    context = GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        change_id="CH-1",
        params={"force_continue": False},
        state_values={},
        node_results={},
        audit_events_dir=change_dir,
        checkpoint_ns="root/codegen/codegen-inv",
    )
    gates = load_workflow_v2(Path.cwd()).gates

    assert check_gate_in_view(gates, precondition_gate, context).verdict.value == "pass"

    data_knowledge.unlink()
    assert check_gate_in_view(gates, precondition_gate, context).verdict.value == "stop"
