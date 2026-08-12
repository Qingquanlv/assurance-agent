"""NodeRunner 分发与 canonical target handler。

覆盖：精确 target 分发与 namespace 默认、未知 target contract 失败、handler
异常归一化 internal；agent 桥的 workspace 请求构建、adapter 失败 kind 透传、
write-set 冻结与 forbidden_write/invalid_output；operation registry（no-op、
skill-registry-check、run-tests、allocate、record-status、stop）；显式
artifact view 上的冻结 gate 求值与 gate/join/interrupt builtin handler；
headless/opencode adapter 的 graph AgentInvoker 路径与 v1→v2 翻译 shim。
"""

from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from assurance_agent.workflow.driver.adapter import (
    PhaseRequest,
    agent_to_phase_result,
    phase_to_agent_request,
)
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.opencode_adapter import OpenCodeAdapter
from assurance_agent.workflow.driver.phase_prompt import build_phase_prompt
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    parse_execution_contracts,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.execution.graph_ops import run_tests
from assurance_agent.workflow.graph.handlers import operation as operation_mod
from assurance_agent.workflow.graph.handlers.agent import AgentHandler, agent_for_skill
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.join import JoinHandler
from assurance_agent.workflow.graph.handlers.operation import OperationHandler, link_host_task_paths
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.healing.graph_ops import (
    operation_allocate_healing_attempt,
    operation_record_healing_status,
    skill_registry_check,
)
from assurance_agent.workflow.report.graph_ops import inspect_operation
from assurance_agent.workflow.graph.model_routing import ModelRouter
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.schema_v2 import (
    RetryPolicyDef,
    TimeoutPolicyDef,
    parse_workflow_v2,
)
from assurance_agent.workflow.graph.task_runner import (
    HandlerNodeRunner,
    build_default_node_runner,
)
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
)
from assurance_agent.workflow.orchestration.gates import (
    GateError,
    GateEvaluationContext,
    check_gate_in_view,
)
import yaml
from assurance_agent.workflow.orchestration.schema import normalize_gates
from tests.helpers_aa import write_aa_config
from assurance_agent.config import ModelRoutingCfg

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, *, skills: bool = False) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(project)
    if skills:
        for name in ("aa-fix-proposal", "aa-api-codegen-fixer", "aa-e2e-codegen-fixer"):
            skill_dir = project / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return project


def _store(project: Path) -> TreeStore:
    return TreeStore(project / "qa" / "changes" / "CH-1")


def _workspace(project: Path, task_id: str = "task-1") -> TaskWorkspace:
    store = _store(project)
    tree_id = store.capture(project)
    return WorkspaceBackend(project / "qa" / "changes" / "CH-1").create(
        task_id=task_id, base_tree_id=tree_id, store=store
    )


def _context(project: Path, *, params: dict[str, object] | None = None) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params=params or {},
    )


def _task(
    target: str,
    *,
    task_id: str = "task-1",
    graph_id: str = "main",
    node_id: str = "node-a",
    input_payload: object | None = None,
    resources: ResourceClaims | None = None,
    run_seconds: float = 60.0,
    prior_error_kind: ErrorKind | None = None,
    contract_failure_kinds_seen: tuple[ErrorKind, ...] = (),
) -> ExecutableTask:
    payload = input_payload if input_payload is not None else {"with": {}, "context": {"change_id": "CH-1"}}
    return ExecutableTask(
        task_id=task_id,
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        graph_id=graph_id,
        node_id=node_id,
        structural_path="main",
        input=payload,
        input_sha256="in-1",
        contract_digest="cd-1",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=run_seconds, heartbeat_seconds=10.0),
        target=target,
        resources=resources or ResourceClaims(),
        prior_error_kind=prior_error_kind,
        contract_failure_kinds_seen=contract_failure_kinds_seen,
    )


def _compiled(body: str, *, footer: str = "gates: {}\n") -> CompiledWorkflow:
    text = (
        'schema_version: "2"\nname: t\n'
        "entrypoints:\n  full: {graph: main}\n"
        "graphs:\n" + textwrap.indent(textwrap.dedent(body), "  ") + footer
    )
    return compile_workflow(parse_workflow_v2(text))


class RecordingInvoker:
    """记录 request 的 fake AgentInvoker；可选在 invoke 期间向 workspace 写一个文件。"""

    def __init__(self, result: AgentResult | None = None, *, write: str | None = None) -> None:
        self.requests: list[AgentRequest] = []
        self._result = result if result is not None else AgentResult(ok=True)
        self._write = write

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if self._write is not None:
            target = Path(request.workspace_root) / self._write
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("agent output\n", encoding="utf-8")
        return self._result


class CanonicalKnowledgeEscapingInvoker(RecordingInvoker):
    """Simulate a headless model escaping its task cwd and rewriting canonical L1."""

    def __init__(self, project: Path) -> None:
        super().__init__(write="qa/changes/CH-1/explore/summary.md")
        self._project = project

    def invoke(self, request: AgentRequest) -> AgentResult:
        result = super().invoke(request)
        (self._project / ".aa" / "data-knowledge.yaml").write_text(
            "version: 1\nentities:\n  dept:\n    constraints:\n      name_has_max_length: 20\n",
            encoding="utf-8",
        )
        return result


class RecordingHandler:
    def __init__(self, result: TaskResult) -> None:
        self.calls: list[tuple[ExecutableTask, TaskWorkspace, RuntimeContext]] = []
        self.result = result
        self.exc: Exception | None = None

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        self.calls.append((task, workspace, context))
        if self.exc is not None:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# Step 1：NodeRunner 分发语义
# ---------------------------------------------------------------------------


def test_dispatches_one_handler_per_namespace_exactly(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    context = _context(project)
    h_skill = RecordingHandler(TaskResult(status="succeeded", value="skill"))
    h_op = RecordingHandler(TaskResult(status="succeeded", value="op"))
    h_builtin = RecordingHandler(TaskResult(status="succeeded", value="builtin"))
    runner = HandlerNodeRunner({"skill:a": h_skill, "operation:b": h_op, "builtin:c": h_builtin})
    for target, handler, value in (
        ("skill:a", h_skill, "skill"),
        ("operation:b", h_op, "op"),
        ("builtin:c", h_builtin, "builtin"),
    ):
        result = runner.execute(_task(target), workspace, context)
        assert result.status == "succeeded"
        assert result.value == value
        assert len(handler.calls) == 1
        called_task, called_workspace, called_context = handler.calls[0]
        assert called_task.target == target
        assert called_workspace is workspace
        assert called_context is context
    # 精确分发：未注册的相邻 target 不落到已注册 handler。
    miss = runner.execute(_task("skill:other"), workspace, context)
    assert miss.error_kind == "contract"
    assert len(h_skill.calls) == 1


def test_unknown_target_is_contract_failure(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    runner = HandlerNodeRunner({})
    result = runner.execute(_task("operation:nope"), _workspace(project), _context(project))
    assert result.status == "failed"
    assert result.error_kind == "contract"
    assert "unknown target" in (result.error or "")


def test_handler_exception_normalized_to_internal_without_escaping(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    handler = RecordingHandler(TaskResult(status="succeeded"))
    handler.exc = RuntimeError("kaboom")
    runner = HandlerNodeRunner({"skill:a": handler})
    result = runner.execute(_task("skill:a"), _workspace(project), _context(project))
    assert result.status == "failed"
    assert result.error_kind == "internal"
    assert "kaboom" in (result.error or "")


def test_skill_namespace_default_dispatches_any_skill(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    handler = RecordingHandler(TaskResult(status="succeeded"))
    runner = HandlerNodeRunner({}, namespace_handlers={"skill": handler})
    result = runner.execute(_task("skill:unregistered"), _workspace(project), _context(project))
    assert result.status == "succeeded"
    assert len(handler.calls) == 1


def test_build_default_node_runner_dispatches_canonical_targets(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled = _compiled(
        """
        main:
          max_supersteps: 5
          nodes:
            noop: {uses: operation:no-op}
            join: {uses: builtin:join, join: {sources: [noop], mode: all}}
          edges:
            - {from: START, to: noop}
            - {from: noop, to: join}
            - {from: join, to: END}
        """
    )
    runner = build_default_node_runner(
        RecordingInvoker(),
        _store(project),
        _agent_catalog(),
        compiled=compiled,
        operations=default_operations(),
    )
    context = _context(project)
    assert runner.execute(_task("operation:no-op"), _workspace(project, "t-1"), context).status == "succeeded"
    assert runner.execute(_task("builtin:join"), _workspace(project, "t-2"), context).status == "succeeded"
    unknown = runner.execute(_task("operation:unknown"), _workspace(project, "t-3"), context)
    assert unknown.status == "failed"
    assert unknown.error_kind == "contract"


# ---------------------------------------------------------------------------
# Step 3：agent handler
# ---------------------------------------------------------------------------

_AGENT_GRAPH = """
main:
  max_supersteps: 5
  nodes:
    explore:
      uses: skill:aa-explore
      outputs: [change:explore/summary.md]
  edges:
    - {from: START, to: explore}
    - {from: explore, to: END}
"""


def _agent_catalog() -> ExecutionContractCatalog:
    return parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:aa-explore:\n"
        "    handler: agent\n"
        "    writes: [change:explore/**]\n"
        "    authorization_writes: [change:explore/**]\n"
        # Packaged explore is declared_only; without this the handler reattaches
        # host .venv/node_modules into the task root (D11/D13).
        "    read_isolation: declared_only\n"
    )


def _agent_handler(project: Path, invoker: RecordingInvoker) -> AgentHandler:
    return AgentHandler(
        invoker,
        _store(project),
        contracts=_agent_catalog(),
        compiled=_compiled(_AGENT_GRAPH),
    )


def _synchronized_agent_handler(project: Path, invoker: RecordingInvoker) -> AgentHandler:
    catalog = parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:aa-explore:\n"
        "    handler: agent\n"
        "    reads: [project:qa/issues/**]\n"
        "    writes: [project:qa/**]\n"
        "    authorization_writes: [project:qa/**]\n"
        "    synchronized: [project:qa/issues/**]\n"
        "    exclusive: [project:issue-registry]\n"
    )
    return AgentHandler(
        invoker,
        _store(project),
        contracts=catalog,
        compiled=_compiled(_AGENT_GRAPH),
    )


def test_agent_handler_builds_workspace_request_and_freezes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / ".venv").mkdir()
    (project / "node_modules").mkdir()
    (project / ".opencode").mkdir()
    (project / "qa/changes/CH-1/events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    workspace = _workspace(project)
    invoker = RecordingInvoker(write="qa/changes/CH-1/explore/summary.md")
    handler = _agent_handler(project, invoker)
    result = handler.execute(_task("skill:aa-explore", node_id="explore"), workspace, _context(project))

    assert result.status == "succeeded"
    assert result.write_set_id is not None
    assert result.outputs_sha256.get("change:explore/summary.md")

    request = invoker.requests[0]
    assert request.target == "skill:aa-explore"
    assert request.node_id == "explore"
    assert request.change_id == "CH-1"
    assert request.workspace_root == workspace.root
    assert request.timeout_seconds == 60.0
    # Declared outputs are always authorized alongside the contract's write scope.
    assert request.allowed_writes == ("change:explore/**", "change:explore/summary.md")
    assert "Authorized write paths: change:explore/**" in request.prompt
    assert "skill(name='aa-explore')" in request.prompt
    # Explore owns the only agent permission for ``aa risk *``.
    assert request.agent == "aa-explorer"
    # The prompt pins the absolute sandbox cwd so a bash-restricted agent cannot
    # "discover" the canonical project root and resolve outputs outside the sandbox.
    assert str(workspace.root) in request.prompt
    assert "IS this task's project root" in request.prompt
    for rel in (".venv", "node_modules", ".opencode", "qa/changes/CH-1/events.jsonl"):
        assert not (workspace.project_root / rel).exists()


def test_agent_handler_restores_and_rejects_canonical_l1_escape_write(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    knowledge = project / ".aa" / "data-knowledge.yaml"
    original = (
        "version: 1\nentities:\n  dept:\n    constraints:\n      name_has_max_length: true\n"
    ).encode()
    knowledge.write_bytes(original)
    workspace = _workspace(project)
    handler = _agent_handler(project, CanonicalKnowledgeEscapingInvoker(project))

    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"),
        workspace,
        _context(project),
    )

    assert result.status == "failed"
    assert result.error_kind == "forbidden_write"
    assert ".aa/data-knowledge.yaml" in (result.error or "")
    assert knowledge.read_bytes() == original


def test_agent_handler_prefers_explicit_node_agent_over_name_inference(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(write="qa/changes/CH-1/explore/summary.md")
    compiled = _compiled(
        """
        main:
          max_supersteps: 5
          nodes:
            explore:
              uses: skill:aa-explore
              agent: aa-reporter
              outputs: [change:explore/summary.md]
          edges:
            - {from: START, to: explore}
            - {from: explore, to: END}
        """
    )
    handler = AgentHandler(
        invoker,
        _store(project),
        contracts=_agent_catalog(),
        compiled=compiled,
    )

    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"),
        _workspace(project),
        _context(project),
    )

    assert result.status == "succeeded"
    assert invoker.requests[0].agent == "aa-reporter"


def test_aa_retro_agent_prompt_has_no_v2_runtime_special_case(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    invoker = RecordingInvoker(write="qa/retro/retro-1/probe.txt")
    catalog = parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:aa-retro:\n"
        "    handler: agent\n"
        "    writes: [project:qa/retro/**]\n"
        "    authorization_writes: [project:qa/retro/**]\n"
    )
    graph = """
    main:
      max_supersteps: 5
      nodes:
        propose:
          uses: skill:aa-retro
          outputs: [project:qa/retro/retro-1/probe.txt]
      edges:
        - {from: START, to: propose}
        - {from: propose, to: END}
    """
    handler = AgentHandler(
        invoker,
        _store(project),
        contracts=catalog,
        compiled=_compiled(graph),
    )
    result = handler.execute(
        _task("skill:aa-retro", node_id="propose"),
        workspace,
        _context(project, params={"retro_id": "retro-1"}),
    )
    assert result.status == "succeeded"
    prompt = invoker.requests[0].prompt
    assert "schema_version='2'" not in prompt
    assert "Read only qa/retro/retro-1/context.json" not in prompt


def test_agent_fan_out_preserves_synchronized_claims_in_frozen_write_set(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    invoker = RecordingInvoker(write="qa/issues/ISSUE-2.json")
    handler = _synchronized_agent_handler(project, invoker)
    task = _task(
        "skill:aa-explore",
        node_id="explore",
        input_payload={
            "resources": {"writes": ["project:qa/issues/**"]},
            "outputs": ["project:qa/issues/ISSUE-2.json"],
        },
    )

    result = handler.execute(task, workspace, _context(project))

    assert result.status == "succeeded"
    assert result.write_set_id is not None
    write_set = _store(project).load_write_set(result.write_set_id)
    assert write_set.synchronized_paths == ("project:qa/issues/**",)
    assert write_set.project_exclusive_tokens == ("project:issue-registry",)


def test_agent_fan_out_cannot_narrow_authorization_outside_synchronized_prefix(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    invoker = RecordingInvoker(write="qa/other/result.json")
    handler = _synchronized_agent_handler(project, invoker)
    task = _task(
        "skill:aa-explore",
        node_id="explore",
        input_payload={
            "resources": {"writes": ["project:qa/other/**"]},
            "outputs": ["project:qa/other/result.json"],
        },
    )

    result = handler.execute(task, workspace, _context(project))

    assert result.status == "failed"
    assert result.error_kind == "forbidden_write"
    assert "synchronized write outside declared prefixes" in (result.error or "")


def test_build_node_prompt_pins_absolute_workspace_root() -> None:
    from assurance_agent.workflow.graph.agent_api import build_node_prompt

    prompt = build_node_prompt(
        "aa-fix-proposal",
        "proposal",
        "CH-1",
        allowed_writes=["change:healing/fix-proposal.json"],
        workspace_root="/ws/tasks/abc",
    )
    assert "Your working directory is EXACTLY '/ws/tasks/abc'" in prompt
    assert "/ws/tasks/abc/qa/changes/CH-1/" in prompt
    # Without workspace_root the concrete cwd clause is omitted.
    bare = build_node_prompt("aa-fix-proposal", "proposal", "CH-1", allowed_writes=["change:healing/**"])
    assert "Your working directory is EXACTLY" not in bare


def test_build_node_prompt_renders_frozen_evidence_clause() -> None:
    from assurance_agent.workflow.graph.agent_api import build_node_prompt

    prompt = build_node_prompt(
        "aa-api-codegen",
        "codegen",
        "CH-1",
        allowed_writes=["change:tests/**"],
        evidence={"plan": {"decision": "approve"}},
    )
    assert "FROZEN UPSTREAM EVIDENCE" in prompt
    assert '"decision": "approve"' in prompt
    # No evidence -> clause omitted.
    bare = build_node_prompt("aa-api-codegen", "codegen", "CH-1", allowed_writes=["change:tests/**"])
    assert "FROZEN UPSTREAM EVIDENCE" not in bare


def test_agent_for_skill_routes_every_workflow_skill() -> None:
    # Authoritative skill -> aa-* worker mapping (see .opencode/agents/*.md
    # "Serves phases"). Wrong routing breaks a node on its permission floor.
    expected = {
        "aa-explore": "aa-explorer",
        "aa-case-design": "aa-doc-author",
        "aa-case-fixer": "aa-doc-author",
        "aa-fact-baseline": "aa-doc-author",
        "aa-api-plan": "aa-doc-author",
        "aa-api-plan-fixer": "aa-doc-author",
        "aa-e2e-plan": "aa-doc-author",
        "aa-e2e-plan-fixer": "aa-doc-author",
        "aa-fuzz-plan": "aa-doc-author",
        "aa-performance-plan": "aa-doc-author",
        "aa-fix-proposal": "aa-doc-author",
        "aa-api-codegen": "aa-test-author",
        "aa-api-codegen-fixer": "aa-test-author",
        "aa-e2e-codegen": "aa-test-author",
        "aa-e2e-codegen-fixer": "aa-test-author",
        "aa-fuzz-codegen": "aa-test-author",
        "aa-performance-codegen": "aa-test-author",
        "aa-case-reviewer": "aa-reviewer",
        "aa-api-plan-reviewer": "aa-reviewer",
        "aa-e2e-plan-reviewer": "aa-reviewer",
        "aa-fuzz-plan-reviewer": "aa-reviewer",
        "aa-performance-plan-reviewer": "aa-reviewer",
        "aa-inspect": "aa-reviewer",
        "aa-report-generator": "aa-reporter",
        "aa-archive": "aa-archiver",
        "aa-issue-analyzer": "aa-doc-author",
        "aa-issue-triage-advisor": "aa-doc-author",
    }
    for skill, agent in expected.items():
        assert agent_for_skill(skill) == agent, skill
    assert agent_for_skill("") is None


@pytest.mark.parametrize(
    ("skill", "expected"),
    [
        ("aa-case-design", "anthropic/glm-5.2"),
        ("aa-case-reviewer", "anthropic/deepseek-v4-flash"),
        ("aa-improvement-reviewer", "anthropic/glm-5.2"),
    ],
)
def test_agent_handler_injects_skill_routed_model(tmp_path: Path, skill: str, expected: str) -> None:
    project = _make_project(tmp_path)
    output = f"change:routing/{skill}.txt"
    catalog = parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        f"  skill:{skill}:\n"
        "    handler: agent\n"
        "    writes: [change:routing/**]\n"
        "    authorization_writes: [change:routing/**]\n"
    )
    compiled = _compiled(
        f"""
        main:
          max_supersteps: 5
          nodes:
            routed:
              uses: skill:{skill}
              outputs: [{output}]
          edges:
            - {{from: START, to: routed}}
            - {{from: routed, to: END}}
        """
    )
    invoker = RecordingInvoker(write=f"qa/changes/CH-1/routing/{skill}.txt")
    router = ModelRouter(
        ModelRoutingCfg.model_validate(
            {
                "strict_routes": True,
                "routes": {
                    "aa-case-design": "anthropic/glm-5.2",
                    "aa-case-reviewer": "anthropic/deepseek-v4-flash",
                    "aa-improvement-reviewer": "anthropic/glm-5.2",
                },
            }
        )
    )
    handler = AgentHandler(
        invoker,
        _store(project),
        contracts=catalog,
        compiled=compiled,
        model_router=router,
        adapter_name="opencode",
    )

    result = handler.execute(
        _task(f"skill:{skill}", node_id="routed"),
        _workspace(project),
        _context(project),
    )

    if skill != "aa-improvement-reviewer":
        assert result.status == "succeeded"
    request = invoker.requests[0]
    assert request.model == expected
    assert request.model_route_source == "skill_route"


def test_agent_handler_escalates_contract_retry_model(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(write="qa/changes/CH-1/explore/summary.md")
    router = ModelRouter(
        ModelRoutingCfg.model_validate(
            {
                "strict_routes": True,
                "routes": {"aa-explore": "anthropic/deepseek-v4-flash"},
                "escalation": {
                    "model": "anthropic/glm-5.2",
                    "on_error_kinds": ["invalid_output", "forbidden_write"],
                },
            }
        )
    )
    handler = AgentHandler(
        invoker,
        _store(project),
        contracts=_agent_catalog(),
        compiled=_compiled(_AGENT_GRAPH),
        model_router=router,
        adapter_name="opencode",
    )

    result = handler.execute(
        _task(
            "skill:aa-explore",
            node_id="explore",
            prior_error_kind="invalid_output",
        ),
        _workspace(project),
        _context(project),
    )

    assert result.status == "succeeded"
    assert invoker.requests[0].model == "anthropic/glm-5.2"
    assert invoker.requests[0].model_route_source == "escalation"


def test_agent_handler_keeps_escalated_model_after_transient_failure(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(write="qa/changes/CH-1/explore/summary.md")
    router = ModelRouter(
        ModelRoutingCfg.model_validate(
            {
                "strict_routes": True,
                "routes": {"aa-explore": "anthropic/deepseek-v4-flash"},
                "escalation": {
                    "model": "anthropic/glm-5.2",
                    "on_error_kinds": ["invalid_output", "forbidden_write"],
                },
            }
        )
    )
    handler = AgentHandler(
        invoker,
        _store(project),
        contracts=_agent_catalog(),
        compiled=_compiled(_AGENT_GRAPH),
        model_router=router,
        adapter_name="opencode",
    )

    result = handler.execute(
        _task(
            "skill:aa-explore",
            node_id="explore",
            prior_error_kind="timeout",
            contract_failure_kinds_seen=("invalid_output",),
        ),
        _workspace(project),
        _context(project),
    )

    assert result.status == "succeeded"
    assert invoker.requests[0].model == "anthropic/glm-5.2"
    assert invoker.requests[0].model_route_source == "escalation"


def test_agent_handler_adapter_failure_preserves_error_kind(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(AgentResult(ok=False, error_kind="rate_limit", error="slow down"))
    handler = _agent_handler(project, invoker)
    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"), _workspace(project), _context(project)
    )
    assert result.status == "failed"
    assert result.error_kind == "rate_limit"
    assert result.error == "slow down"


def test_agent_handler_adapter_failure_defaults_internal(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(AgentResult(ok=False, error="boom"))
    handler = _agent_handler(project, invoker)
    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"), _workspace(project), _context(project)
    )
    assert result.status == "failed"
    assert result.error_kind == "internal"
    assert result.error == "boom"


def test_agent_handler_forbidden_write_fails_closed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker(write="qa/changes/CH-1/other/escape.txt")
    handler = _agent_handler(project, invoker)
    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"), _workspace(project), _context(project)
    )
    assert result.status == "failed"
    assert result.error_kind == "forbidden_write"


def test_agent_handler_missing_declared_output_is_invalid_output(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    invoker = RecordingInvoker()  # ok 但不写声明的 output
    handler = _agent_handler(project, invoker)
    result = handler.execute(
        _task("skill:aa-explore", node_id="explore"), _workspace(project), _context(project)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_issue_analyzer_digest_is_runtime_owned_and_frozen_with_outputs(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    candidate_document = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [],
    }

    class IssueInvoker:
        def invoke(self, request: AgentRequest) -> AgentResult:
            inspect_dir = Path(request.workspace_root) / "qa" / "changes" / "CH-1" / "inspect"
            inspect_dir.mkdir(parents=True, exist_ok=True)
            (inspect_dir / "issue-candidates.json").write_text(
                json.dumps(candidate_document), encoding="utf-8"
            )
            # The analyzer authors semantic status only. The deterministic digest
            # is an engine-owned cross-output field and is intentionally omitted.
            (inspect_dir / "issue-analysis-status.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "change_id": "CH-1",
                        "batch_id": "batch-1",
                        "status": "completed",
                        "evidence_bundle_digest": "sha256:evidence",
                        "candidate_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            return AgentResult(ok=True)

    catalog = parse_execution_contracts(
        'schema_version: "1"\n'
        "contracts:\n"
        "  skill:aa-issue-analyzer:\n"
        "    handler: agent\n"
        "    writes: [change:inspect/issue-candidates.json, "
        "change:inspect/issue-analysis-status.json]\n"
        "    authorization_writes: [change:inspect/issue-candidates.json, "
        "change:inspect/issue-analysis-status.json]\n"
    )
    graph = """
    main:
      max_supersteps: 5
      nodes:
        analyze:
          uses: skill:aa-issue-analyzer
          outputs:
            - change:inspect/issue-candidates.json
            - change:inspect/issue-analysis-status.json
      edges:
        - {from: START, to: analyze}
        - {from: analyze, to: END}
    """
    handler = AgentHandler(
        IssueInvoker(),
        _store(project),
        contracts=catalog,
        compiled=_compiled(graph),
    )

    result = handler.execute(
        _task("skill:aa-issue-analyzer", node_id="analyze"),
        workspace,
        _context(project),
    )

    assert result.status == "succeeded"
    status = json.loads(
        (workspace.change_dir / "inspect" / "issue-analysis-status.json").read_text(encoding="utf-8")
    )
    from assurance_agent.workflow.issues.identity import candidate_document_digest

    assert status["candidate_digest"] == candidate_document_digest(candidate_document)
    assert result.outputs_sha256["change:inspect/issue-analysis-status.json"]


# ---------------------------------------------------------------------------
# Step 4：operation handler registry
# ---------------------------------------------------------------------------


def test_default_operations_registry_has_exact_keys() -> None:
    assert set(default_operations()) == {
        "operation:no-op",
        "operation:skill-registry-check",
        "operation:verify-plan-mechanical",
        "operation:derive-plan-layer-applicability",
        "operation:run-tests",
        "operation:allocate-healing-attempt",
        "operation:fixer-authority-ready",
        "operation:record-fixer-approval",
        "operation:fixer-dispatch",
        "operation:record-codegen-fix-apply",
        "operation:combine-fixer-safety",
        "operation:record-healing-status",
        "operation:probe-coverage-repair-need",
        "operation:compute-coverage-repair-safety",
        "operation:allocate-coverage-repair-attempt",
        "operation:record-coverage-repair-status",
        "operation:inspect",
        "operation:generate-report",
        "operation:stop",
        "operation:retro-collect-v3",
        "operation:assemble-retro-context-v3",
        "operation:drain-improvement-outbox",
        "operation:finalize-retro-status",
        "operation:record-retro-pipeline-failure",
        "operation:retro-evidence-gap-fallback",
        "operation:record-analysis-failed",
        "operation:materialize-empty-retro-analysis",
        "operation:reconcile-improvements",
        "operation:load-review-subject",
        "operation:validate-improvement-review-assessment",
        "operation:apply-improvement-auto-review",
        "operation:record-improvement-auto-review-error",
        "operation:record-auto-review-orchestration-error",
        "operation:select-current-retro-auto-review-items",
        "operation:summarize-auto-review-batch",
        "operation:retro-accept",  # half-cutover alias
        "operation:collect-observations",
        "operation:record-empty-issue-analysis",
        "operation:record-issue-analysis-failure",
        "operation:record-project-sync-pending",
        # Issue lifecycle (Task 9-11)
        "operation:reconcile-issues",
        # Reconciled trace projection + the independent trace-sufficiency gate
        "operation:materialize-trace-projection",
        # Issue review (Task 12)
        "operation:load-problem-review-context",
        "operation:apply-problem-review",
        # Improvement review (retro/improvement separation Task 10)
        "operation:load-improvement-review-context",
        "operation:apply-improvement-review",
        # Improvement delivery (retro/improvement separation Task 11)
        "operation:load-improvement-delivery",
        "operation:evaluate-memory-improvement",
        "operation:apply-memory-improvement",
        "operation:rollback-memory-improvement",
        "operation:export-change-improvement",
        "operation:record-change-improvement-applied",
        "operation:export-knowledge-improvement",
        "operation:record-knowledge-improvement-applied",
        # Verification metrics M1 (Tasks 5–7): MRC join + PR cadence collectors
        "operation:materialize-minimum-coverage",
        "operation:collect-diff-coverage",
        "operation:compute-constraint-coverage",
        "operation:compute-auth-matrix",
        "operation:compute-journey-coverage",
        "operation:compute-threshold-slack",
        "operation:collect-pr-metrics-batch",
        "operation:materialize-pr-metrics",
        # Nightly metrics carrier (metrics M2 Task 1)
        "operation:load-latest-pr-metrics",
        "operation:run-mutation-sample",
        "operation:compute-assertion-strength",
        "operation:compute-baseline-drift",
        "operation:aggregate-nightly-metrics",
        "operation:evaluate-retrospective-shortboards",
        # Adversarial discovery yield + flaky quarantine (metrics M3 Tasks 2/4)
        "operation:collect-adversarial-yield",
        "operation:materialize-quarantine-projection",
        # Dual-source Lane B gap signals and the report-only C-layer aggregate (M4)
        "operation:build-coverage-gap-signals",
        "operation:materialize-c-layer-metrics",
    }


def test_operation_handler_unknown_operation_is_contract_failure(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    handler = OperationHandler(default_operations())
    result = handler.execute(_task("operation:bogus"), _workspace(project), _context(project))
    assert result.status == "failed"
    assert result.error_kind == "contract"


def test_no_op_succeeds(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = operation_mod.no_op(_task("operation:no-op"), _workspace(project), _context(project))
    assert result.status == "succeeded"


def test_stop_returns_stopped_with_declared_reason(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    task = _task(
        "operation:stop",
        input_payload={"with": {"reason": "case fix attempts exhausted"}, "context": {}},
    )
    result = operation_mod.stop_operation(task, _workspace(project), _context(project))
    assert result.status == "stopped"
    assert result.value == {"reason": "case fix attempts exhausted"}


def test_stop_requires_reason(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = operation_mod.stop_operation(_task("operation:stop"), _workspace(project), _context(project))
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_skill_registry_check(tmp_path: Path) -> None:
    project = _make_project(tmp_path, skills=True)
    workspace = _workspace(project)
    result = skill_registry_check(
        _task("operation:skill-registry-check"),
        workspace,
        _context(project, params={"max_healing_attempts": 3}),
    )
    assert result.status == "succeeded"
    assert result.value == {"healing_available": True, "status": "pass"}
    assert (workspace.change_dir / "registry" / "skill-registry-check.json").is_file()

    zero_budget = skill_registry_check(
        _task("operation:skill-registry-check"),
        _workspace(project),
        _context(project, params={"max_healing_attempts": 0}),
    )
    assert zero_budget.value == {"healing_available": False, "status": "fail"}

    bare = _make_project(tmp_path / "bare", skills=False)
    missing = skill_registry_check(
        _task("operation:skill-registry-check"),
        _workspace(bare),
        _context(bare, params={"max_healing_attempts": 3}),
    )
    assert missing.value == {"healing_available": False, "status": "fail"}


def test_run_tests_invokes_run_change_against_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path)
    (project / ".venv").mkdir()
    (project / "node_modules").mkdir()
    workspace = _workspace(project)
    calls: dict[str, Path] = {}

    class _Manifest:
        batch_id = "b-1"
        final_status = "PASS"

    def fake_run_change(project_root: Path, change_dir: Path, config: object, **kwargs: object) -> _Manifest:
        calls["project_root"] = project_root
        calls["change_dir"] = change_dir
        execution = change_dir / "execution"
        execution.mkdir(parents=True, exist_ok=True)
        (execution / "execution-manifest.yaml").write_text("batch_id: b-1\n", encoding="utf-8")
        return _Manifest()

    monkeypatch.setattr("assurance_agent.workflow.execution.graph_ops.run_change", fake_run_change)
    result = run_tests(_task("operation:run-tests"), workspace, _context(project))

    assert result.status == "succeeded"
    assert result.value == {"batch_id": "b-1", "final_status": "PASS"}
    assert calls["project_root"] == workspace.project_root
    assert calls["change_dir"] == workspace.change_dir
    assert (workspace.project_root / ".venv").is_symlink()
    assert (workspace.project_root / ".venv").resolve() == (project / ".venv").resolve()
    assert (workspace.project_root / "node_modules").is_symlink()


def test_workspace_capture_includes_python_version(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / ".python-version").write_text("3.11\n", encoding="utf-8")
    workspace = _workspace(project)
    captured = workspace.project_root / ".python-version"
    assert captured.is_file()
    assert not captured.is_symlink()
    assert captured.read_text(encoding="utf-8").strip() == "3.11"


def test_inspect_operation_writes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_project(tmp_path)
    execution = project / "qa" / "changes" / "CH-1" / "execution"
    execution.mkdir(parents=True, exist_ok=True)
    (execution / "execution-manifest.yaml").write_text("batch_id: b-1\n", encoding="utf-8")

    class _InspectResult:
        class _Analysis:
            batch_id = "b-1"
            final_status = "SKIPPED"
            status = "no_failures"

        analysis = _Analysis()

    monkeypatch.setattr(
        "assurance_agent.workflow.report.graph_ops.inspect_change",
        lambda *_a, **_k: _InspectResult(),
    )
    workspace = _workspace(project)
    result = inspect_operation(_task("operation:inspect"), workspace, _context(project))

    assert result.status == "succeeded"
    assert result.value == {"batch_id": "b-1", "final_status": "SKIPPED", "status": "no_failures"}


def _write_execution_and_proposal(project: Path) -> None:
    change = project / "qa" / "changes" / "CH-1"
    execution = change / "execution"
    execution.mkdir(parents=True, exist_ok=True)
    (execution / "execution-manifest.yaml").write_text("batch_id: 20260719-120000\n", encoding="utf-8")
    healing = change / "healing"
    healing.mkdir(parents=True, exist_ok=True)
    (healing / "fix-proposal.json").write_text(json.dumps({"proposals": []}), encoding="utf-8")


def test_allocate_healing_attempt_writes_baseline_and_status(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_execution_and_proposal(project)
    workspace = _workspace(project)
    result = operation_allocate_healing_attempt(
        _task("operation:allocate-healing-attempt"), workspace, _context(project)
    )

    assert result.status == "succeeded"
    baseline = workspace.change_dir / "healing" / "entry-baseline.json"
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["entry_batch_id"] == "20260719-120000"
    proposal_sha = hashlib.sha256(
        (workspace.change_dir / "healing" / "fix-proposal.json").read_bytes()
    ).hexdigest()
    expected_episode = hashlib.sha256(f"CH-1:20260719-120000:{proposal_sha}".encode()).hexdigest()
    assert payload["episode_id"] == expected_episode

    assert isinstance(result.value, dict)
    assert result.value["attempt_id"] == f"ha-{expected_episode[:12]}-1"
    assert result.value["attempt_number"] == 1
    assert result.value["source_batch_id"] == "20260719-120000"

    status = json.loads((workspace.change_dir / "healing" / "status.json").read_text(encoding="utf-8"))
    assert status["attempts_used"] == 1
    assert status["status"] == "pending"
    assert status["latest_attempt_id"] == result.value["attempt_id"]

    from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger
    from assurance_agent.workflow.core.events import read_events

    assert isinstance(result.value, dict)
    committed = commit_healing_allocation_ledger(
        project / "qa" / "changes" / "CH-1",
        episode_id=str(result.value["episode_id"]),
        attempt_id=str(result.value["attempt_id"]),
        attempt_number=int(result.value["attempt_number"]),
        operation_id=str(result.value["operation_id"]),
        source_batch_id=str(result.value["source_batch_id"]),
        baseline_sha256=str(result.value["baseline_sha256"]),
        entry_batch_id=str(result.value["entry_batch_id"]),
    )
    assert committed is True
    types = [e["type"] for e in read_events(project / "qa" / "changes" / "CH-1")]
    assert types == ["healing_entry_baseline_pinned", "healing_attempt_allocated"]
    assert (
        commit_healing_allocation_ledger(
            project / "qa" / "changes" / "CH-1",
            episode_id=str(result.value["episode_id"]),
            attempt_id=str(result.value["attempt_id"]),
            attempt_number=int(result.value["attempt_number"]),
            operation_id=str(result.value["operation_id"]),
            source_batch_id=str(result.value["source_batch_id"]),
            baseline_sha256=str(result.value["baseline_sha256"]),
            entry_batch_id=str(result.value["entry_batch_id"]),
        )
        is False
    )


def test_link_host_task_paths_symlinks_events_jsonl(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    host_agents = project / ".opencode" / "agents"
    host_agents.mkdir(parents=True)
    (host_agents / "aa-explorer.md").write_text("---\nname: aa-explorer\n---\n", encoding="utf-8")
    host_change = project / "qa" / "changes" / "CH-1"
    host_change.mkdir(parents=True, exist_ok=True)
    (host_change / "events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    workspace = _workspace(project)
    link_host_task_paths(workspace, _context(project))
    task_events = workspace.change_dir / "events.jsonl"
    assert task_events.is_symlink()
    assert task_events.resolve() == (host_change / "events.jsonl").resolve()
    task_opencode = workspace.project_root / ".opencode"
    assert task_opencode.is_symlink()
    assert task_opencode.resolve() == (project / ".opencode").resolve()
    assert (task_opencode / "agents" / "aa-explorer.md").is_file()


def test_subgraph_does_not_project_host_runtime_paths_into_child(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    host_agents = project / ".opencode" / "agents"
    host_agents.mkdir(parents=True)
    (host_agents / "aa-explorer.md").write_text("---\nname: aa-explorer\n---\n", encoding="utf-8")
    workspace = _workspace(project)
    observed: dict[str, Path] = {}

    def run_child(
        task: ExecutableTask,
        graph_id: str,
        child_workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        assert task.target == "graph:intake"
        assert graph_id == "intake"
        observed["opencode"] = child_workspace.project_root / ".opencode"
        return TaskResult(status="succeeded")

    result = SubgraphHandler(run_child).execute(_task("graph:intake"), workspace, _context(project))

    assert result.status == "succeeded"
    task_opencode = observed["opencode"]
    assert not task_opencode.exists()


def test_allocate_healing_attempt_requires_execution_batch(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = operation_allocate_healing_attempt(
        _task("operation:allocate-healing-attempt"), _workspace(project), _context(project)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_record_healing_status(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    task = _task(
        "operation:record-healing-status",
        input_payload={"with": {"status": "resolved"}, "context": {}},
    )
    result = operation_record_healing_status(task, workspace, _context(project))
    assert result.status == "succeeded"
    assert result.value == {"healing_status": "resolved"}
    status = json.loads((workspace.change_dir / "healing" / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "resolved"

    bogus = _task(
        "operation:record-healing-status",
        input_payload={"with": {"status": "bogus"}, "context": {}},
    )
    rejected = operation_record_healing_status(bogus, workspace, _context(project))
    assert rejected.status == "failed"
    assert rejected.error_kind == "invalid_input"


def test_record_healing_status_preserves_allocated_attempt_identity(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    status_path = workspace.change_dir / "healing" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "pending",
                "attempts_used": 1,
                "all_fixers_no_op": False,
                "latest_attempt_id": "ha-episode-1",
                "source_batch_id": "batch-1",
            }
        ),
        encoding="utf-8",
    )

    result = operation_record_healing_status(
        _task(
            "operation:record-healing-status",
            input_payload={"with": {"status": "failed"}, "context": {}},
        ),
        workspace,
        _context(project),
    )

    assert result.status == "succeeded"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["attempts_used"] == 1
    assert status["latest_attempt_id"] == "ha-episode-1"
    assert status["source_batch_id"] == "batch-1"


# ---------------------------------------------------------------------------
# Step 5：显式 artifact view 上的冻结 gate 求值
# ---------------------------------------------------------------------------


class _GateSchema:
    def __init__(self, gates: dict) -> None:
        self.gates = gates


def _gates_schema(text: str) -> _GateSchema:
    doc = yaml.safe_load(text)
    return _GateSchema(normalize_gates(doc.get("gates") or {}))


_GATES = _gates_schema("""
schema_version: "1"
name: t
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    needs_human_review_when: "decision == 'needs_human_review'"
    reject_when: "decision == 'reject'"
    pass_when: "decision == 'pass'"
  file-only-gate:
    reads: [review/case-review.json]
    missing_file_is: reject
    pass_when: "decision == 'pass'"
""")


def _write_review(change_dir: Path, payload: dict[str, object]) -> None:
    review_dir = change_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "case-review.json").write_text(json.dumps(payload), encoding="utf-8")


def _eval_context(project: Path) -> GateEvaluationContext:
    return GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
    )


def test_check_gate_in_view_pass_hashes_audited_reads(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"decision": "pass", "auto_fix_allowed": False})
    report = check_gate_in_view(_GATES.gates, "case-review-gate", _eval_context(project))
    assert report.verdict == "pass"
    assert report.matched_rule == "pass_when: decision == 'pass'"
    expected = hashlib.sha256(
        (project / "qa" / "changes" / "CH-1" / "review" / "case-review.json").read_bytes()
    ).hexdigest()
    assert report.reads_sha256 == {"review/case-review.json": expected}


def test_check_gate_in_view_declaration_order_first_true_wins(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"decision": "needs_fix", "auto_fix_allowed": True})
    report = check_gate_in_view(_GATES.gates, "case-review-gate", _eval_context(project))
    assert report.verdict == "needs_fix"
    assert report.matched_rule is not None and report.matched_rule.startswith("needs_fix_when:")


def test_check_gate_in_view_missing_field_and_missing_file(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"auto_fix_allowed": True})
    missing_field = check_gate_in_view(_GATES.gates, "case-review-gate", _eval_context(project))
    assert missing_field.verdict == "stop"
    assert missing_field.matched_rule == "missing_field"

    bare = _make_project(tmp_path / "bare")  # review 文件整体缺失
    missing_file = check_gate_in_view(_GATES.gates, "file-only-gate", _eval_context(bare))
    assert missing_file.verdict == "reject"
    assert missing_file.matched_rule == "missing_file"
    assert missing_file.reads_sha256 == {}


def test_check_gate_in_view_unknown_gate_raises(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with pytest.raises(GateError, match="unknown gate"):
        check_gate_in_view(_GATES.gates, "nope", _eval_context(project))


# ---------------------------------------------------------------------------
# Step 6：builtin gate / join / interrupt handler
# ---------------------------------------------------------------------------

_GATE_FOOTER = """gates:
  case-review-gate:
    reads: [review/case-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_fix_when: "case_review.decision == 'needs_fix' and case_review.auto_fix_allowed == true"
    needs_human_review_when: "case_review.decision == 'needs_human_review'"
    reject_when: "case_review.decision == 'reject'"
    pass_when: "case_review.decision == 'pass'"
"""


def _gate_task(node_id: str, with_params: dict[str, object]) -> ExecutableTask:
    return _task(
        "builtin:gate",
        node_id=node_id,
        input_payload={"with": with_params, "context": {"change_id": "CH-1"}},
    )


def test_gate_handler_named_gate_business_success_regardless_of_verdict(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"decision": "reject", "auto_fix_allowed": False})
    workspace = _workspace(project)
    handler = GateHandler(
        _compiled(
            """
            main:
              max_supersteps: 5
              nodes:
                review:
                  uses: builtin:gate
                  with: {gate: case-review-gate}
              edges:
                - {from: START, to: review}
                - {from: review, to: END}
            """,
            footer=_GATE_FOOTER,
        )
    )
    result = handler.execute(_gate_task("review", {"gate": "case-review-gate"}), workspace, _context(project))
    assert result.status == "succeeded"
    assert result.value == "reject"
    assert result.gate_report is not None
    assert result.gate_report["gate_id"] == "case-review-gate"
    assert result.gate_report["verdict"] == "reject"
    assert result.gate_report["matched_rule"] == "reject_when: case_review.decision == 'reject'"
    assert result.gate_report["reads_sha256"] != {}


def test_gate_handler_unknown_gate_is_contract_failure(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    handler = GateHandler(
        _compiled(
            """
            main:
              max_supersteps: 5
              nodes:
                review:
                  uses: builtin:gate
                  with: {gate: case-review-gate}
              edges:
                - {from: START, to: review}
                - {from: review, to: END}
            """,
            footer=_GATE_FOOTER,
        )
    )
    result = handler.execute(_gate_task("review", {"gate": "nope"}), _workspace(project), _context(project))
    assert result.status == "failed"
    assert result.error_kind == "contract"


def test_gate_handler_expression_gate(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"decision": "pass"})
    workspace = _workspace(project)
    handler = GateHandler(
        _compiled(
            """
            main:
              max_supersteps: 5
              nodes:
                eligible:
                  uses: builtin:gate
                  with:
                    expression: "file_exists('review/case-review.json')"
              edges:
                - {from: START, to: eligible}
                - {from: eligible, to: END}
            """
        )
    )
    expression = "file_exists('review/case-review.json')"
    result = handler.execute(_gate_task("eligible", {"expression": expression}), workspace, _context(project))
    assert result.status == "succeeded"
    assert result.value is True
    assert result.gate_report == {"expression": expression, "value": True}


def test_gate_handler_expression_gate_missing_file_fail_closed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    workspace = _workspace(project)
    handler = GateHandler(
        _compiled(
            """
            main:
              max_supersteps: 5
              nodes:
                eligible:
                  uses: builtin:gate
                  with:
                    expression: "file_exists('review/case-review.json')"
              edges:
                - {from: START, to: eligible}
                - {from: eligible, to: END}
            """
        )
    )
    result = handler.execute(
        _gate_task("eligible", {"expression": "file_exists('review/case-review.json')"}),
        workspace,
        _context(project),
    )
    assert result.status == "succeeded"
    assert result.value is False


def test_join_handler_returns_succeeded(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = JoinHandler().execute(_task("builtin:join"), _workspace(project), _context(project))
    assert result.status == "succeeded"


_INTERRUPT_GRAPH = """
main:
  max_supersteps: 5
  nodes:
    review:
      uses: builtin:gate
      with: {gate: case-review-gate}
    human-review:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
  edges:
    - {from: START, to: review}
  routes:
    - from: review
      select: "node('review').gate.verdict"
      cases:
        needs_human_review: human-review
        pass: END
      default: STOP
    - from: human-review
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_interrupt_handler_builds_structural_projection_without_ledger(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_review(project / "qa" / "changes" / "CH-1", {"decision": "needs_human_review"})
    workspace = _workspace(project)
    handler = InterruptHandler(_compiled(_INTERRUPT_GRAPH, footer=_GATE_FOOTER), _store(project))
    task = _task("builtin:interrupt", node_id="human-review", task_id="task-int")
    result = handler.execute(task, workspace, _context(project))

    assert result.status == "interrupted"
    projection = result.interrupt
    assert projection is not None
    assert len(projection.interrupt_id) == 64
    assert projection.checkpoint_ns == "inv-1"
    assert projection.node_id == "human-review"
    assert projection.checkpoint == "case-review-gate"
    assert projection.actions == ("fix_and_proceed", "accept_risk", "stop")
    expected = hashlib.sha256((workspace.change_dir / "review" / "case-review.json").read_bytes()).hexdigest()
    assert projection.audited_reads_sha256 == {"review/case-review.json": expected}
    assert projection.resolved_action is None
    # handler 不写 ledger；graph_interrupted 由 GraphRuntime 在 sibling settle 后发布。
    assert not (workspace.change_dir / "events.jsonl").exists()

    replay = handler.execute(task, workspace, _context(project))
    assert replay.interrupt is not None
    assert replay.interrupt.interrupt_id == projection.interrupt_id


_MANUAL_REVISION_INTERRUPT_GRAPH = """
main:
  max_supersteps: 5
  nodes:
    human-review:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
        manual_revision:
          action: fix_and_proceed
          paths: [change:plans/fuzz-plan.md]
  edges:
    - {from: START, to: human-review}
  routes:
    - from: human-review
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_interrupt_handler_fails_closed_when_manual_revision_lacks_gate_epoch(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    _write_review(change, {"decision": "needs_human_review"})
    plans = change / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fuzz-plan.md").write_text("# plan\n", encoding="utf-8")
    workspace = _workspace(project)
    handler = InterruptHandler(
        _compiled(_MANUAL_REVISION_INTERRUPT_GRAPH, footer=_GATE_FOOTER),
        _store(project),
    )
    task = _task("builtin:interrupt", node_id="human-review", task_id="task-int")
    result = handler.execute(task, workspace, _context(project))

    assert result.status == "failed"
    assert result.error_kind == "contract"
    assert result.error is not None
    assert "cannot bind a gate evidence epoch" in result.error
    assert not (change / ".graph-runtime" / "revision-views").exists()


# ---------------------------------------------------------------------------
# Step 2：driver adapter 的 graph AgentInvoker 路径与翻译 shim
# ---------------------------------------------------------------------------


def _agent_request(workspace_root: Path, **overrides: object) -> AgentRequest:
    fields: dict[str, object] = {
        "target": "skill:aa-explore",
        "node_id": "explore",
        "change_id": "CH-1",
        "workspace_root": workspace_root,
        "allowed_writes": ("change:explore/**",),
        "prompt": "do it",
        "timeout_seconds": 30.0,
    }
    fields.update(overrides)
    return AgentRequest.model_validate(fields)


def test_phase_request_translation_shim() -> None:
    request = phase_to_agent_request(
        PhaseRequest(change_id="CH-1", phase_id="explore", skill="aa-explore", prompt="p"),
        workspace_root=Path("/ws/task-1"),
        allowed_writes=["change:explore/**"],
        timeout_seconds=12.0,
    )
    assert request.target == "skill:aa-explore"
    assert request.node_id == "explore"
    assert request.change_id == "CH-1"
    assert request.workspace_root == Path("/ws/task-1")
    assert request.allowed_writes == ("change:explore/**",)
    assert request.timeout_seconds == 12.0

    back = agent_to_phase_result(AgentResult(ok=False, error_kind="timeout", error="boom"))
    assert back.ok is False
    assert back.error == "boom"


def test_v2_prompt_lists_authorized_write_roots() -> None:
    prompt = build_phase_prompt(
        "aa-explore",
        "explore",
        "CH-1",
        allowed_writes=["repo:tests/api/**", "change:explore/**"],
        item="menu",
    )
    assert "Authorized write paths: change:explore/**, repo:tests/api/**" in prompt
    assert "Fan-out item: menu." in prompt
    assert "qa/changes/CH-1" not in prompt  # 不再宣称只能写 change 目录
    assert "coordinator runtime files" in prompt


def test_headless_invoke_runs_process_at_workspace_root(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("import pathlib; pathlib.Path('out.txt').write_text('ran')", encoding="utf-8")
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {script}", cwd=tmp_path)
    result = adapter.invoke(_agent_request(workspace_root))
    assert result.ok is True
    assert (workspace_root / "out.txt").read_text(encoding="utf-8") == "ran"
    assert not (tmp_path / "out.txt").exists()


def test_headless_invoke_error_kind_mapping(tmp_path: Path) -> None:
    failing = tmp_path / "fail.py"
    failing.write_text("import sys; sys.exit(2)", encoding="utf-8")
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {failing}", cwd=tmp_path)
    internal = adapter.invoke(_agent_request(tmp_path))
    assert internal.ok is False
    assert internal.error_kind == "internal"

    sleeping = tmp_path / "sleep.py"
    sleeping.write_text("import time; time.sleep(5)", encoding="utf-8")
    slow = HeadlessAdapter(agent_cmd=f"{sys.executable} {sleeping}", cwd=tmp_path)
    timed_out = slow.invoke(_agent_request(tmp_path, timeout_seconds=0.3))
    assert timed_out.ok is False
    assert timed_out.error_kind == "timeout"

    missing = HeadlessAdapter(agent_cmd="/nonexistent/agent-binary-xyz", cwd=tmp_path)
    transport = missing.invoke(_agent_request(tmp_path))
    assert transport.ok is False
    assert transport.error_kind == "transport"


class _StatusScript:
    """Serves POST /session, POST prompt_async (204), and a scripted GET status."""

    def __init__(self, statuses: list[str], session_id: str = "ses_1") -> None:
        self._statuses = statuses
        self._sid = session_id
        self.status_calls = 0
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/session":
            return httpx.Response(200, json={"id": self._sid})
        if path == f"/session/{self._sid}/prompt_async":
            return httpx.Response(204)
        if path == "/session/status":
            i = min(self.status_calls, len(self._statuses) - 1)
            self.status_calls += 1
            st = self._statuses[i]
            return httpx.Response(200, json=({self._sid: {"type": st}} if st != "idle" else {}))
        if path == f"/session/{self._sid}/message":
            return httpx.Response(200, json=[])
        if path == f"/session/{self._sid}/abort":
            return httpx.Response(204)
        return httpx.Response(404)


def _mock_client(handler) -> httpx.Client:  # noqa: ANN001 - httpx MockTransport handler
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_opencode_invoke_uses_workspace_directory_and_returns_session_id(tmp_path: Path) -> None:
    script = _StatusScript(["idle"] * 10)
    adapter = OpenCodeAdapter(
        "http://host", "/sut", client=_mock_client(script), idle_done_streak=1, sleep=lambda _s: None
    )
    result = adapter.invoke(_agent_request(tmp_path / "ws", reconnect_session_id="ses_parent"))
    assert result.ok is True
    assert result.session_id == "ses_1"
    assert script.seen, "expected recorded requests"
    for request in script.seen:
        assert request.url.params["directory"] == str(tmp_path / "ws")
    create_body = json.loads(script.seen[0].content)
    assert create_body["title"] == "Phase explore"
    assert create_body["parentID"] == "ses_parent"


@pytest.mark.parametrize(
    ("status_code", "kind"),
    [(401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "internal")],
)
def test_opencode_invoke_http_error_mapping(tmp_path: Path, status_code: int, kind: str) -> None:
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_mock_client(lambda _r: httpx.Response(status_code, text="nope")),
        idle_done_streak=1,
        sleep=lambda _s: None,
    )
    result = adapter.invoke(_agent_request(tmp_path))
    assert result.ok is False
    assert result.error_kind == kind


def test_opencode_invoke_network_error_is_transport(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OpenCodeAdapter(
        "http://host", "/sut", client=_mock_client(handler), idle_done_streak=1, sleep=lambda _s: None
    )
    result = adapter.invoke(_agent_request(tmp_path))
    assert result.ok is False
    assert result.error_kind == "transport"


def test_opencode_invoke_poll_deadline_is_timeout(tmp_path: Path) -> None:
    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            value = self.t
            self.t += 1.0
            return value

    script = _StatusScript(["busy"] * 100)
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_mock_client(script),
        idle_done_streak=2,
        sleep=lambda _s: None,
        monotonic=_Clock(),
    )
    result = adapter.invoke(_agent_request(tmp_path, timeout_seconds=3.0))
    assert result.ok is False
    assert result.error_kind == "timeout"
    assert result.session_id == "ses_1"
    assert sum(request.url.path == "/session/ses_1/abort" for request in script.seen) == 1
