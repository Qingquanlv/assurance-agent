"""Runtime construction tests for OpenCode phase model routing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.driver.runtime_factory import (
    assemble_graph_runtime,
    build_graph_runtime,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.model_routing import ModelRoutingError
from assurance_agent.workflow.graph.models import RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from tests.helpers_aa import write_aa_config


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected invoke: {request}")


def _prepare(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return tmp_path


def _write_routing_config(project: Path, *, routes: dict[str, str]) -> None:
    path = project / ".aa" / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["execution"]["model_routing"] = {
        "strict_routes": True,
        "routes": routes,
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


_STRICT_SCHEMA = textwrap.dedent(
    """\
    name: strict-test
    entrypoints:
      full: {graph: main}
    policies:
      retry:
        never: {max_attempts: 1, retry_on: []}
      timeout:
        local: {run_seconds: 60, heartbeat_seconds: 10}
      scheduler: {max_parallel_tasks: 2}
    graphs:
      main:
        max_supersteps: 5
        nodes:
          plan:
            uses: skill:aa-api-plan
            retry: never
            timeout: local
            outputs: [change:plans/api-plan.md]
        edges:
          - {from: START, to: plan}
          - {from: plan, to: END}
    gates: {}
    """
)


def test_strict_routes_fail_before_agent_invocation(tmp_path: Path) -> None:
    project = _prepare(tmp_path)
    schema = project / "workflow.yaml"
    schema.write_text(_STRICT_SCHEMA, encoding="utf-8")
    _write_routing_config(project, routes={})

    with pytest.raises(ModelRoutingError, match="aa-api-plan"):
        build_graph_runtime(
            project_root=project,
            change_id="CH-1",
            adapter=NeverCalledInvoker(),
            explicit_schema=schema,
            adapter_name="opencode",
        )


def test_cli_override_bypasses_strict_route_coverage(tmp_path: Path) -> None:
    project = _prepare(tmp_path)
    schema = project / "workflow.yaml"
    schema.write_text(_STRICT_SCHEMA, encoding="utf-8")
    _write_routing_config(project, routes={})

    bundle = build_graph_runtime(
        project_root=project,
        change_id="CH-1",
        adapter=NeverCalledInvoker(),
        explicit_schema=schema,
        adapter_name="opencode",
        cli_model_override="anthropic/glm-5.2",
    )

    assert bundle.compiled.schema.name == "strict-test"


def test_headless_ignores_opencode_strict_routes(tmp_path: Path) -> None:
    project = _prepare(tmp_path)
    schema = project / "workflow.yaml"
    schema.write_text(_STRICT_SCHEMA, encoding="utf-8")
    _write_routing_config(project, routes={})

    bundle = build_graph_runtime(
        project_root=project,
        change_id="CH-1",
        adapter=NeverCalledInvoker(),
        explicit_schema=schema,
        adapter_name="headless",
    )

    assert bundle.compiled.schema.name == "strict-test"


_MINIMAL_CONTRACTS = textwrap.dedent(
    """\
    schema_version: "1"
    contracts:
      operation:no-op:
        handler: operation
        side_effect_free: true
    """
)

_MINIMAL_WORKFLOW = textwrap.dedent(
    """\
    name: assemble-min
    entrypoints:
      full: {graph: main}
    policies:
      retry:
        never: {max_attempts: 1, retry_on: []}
      timeout:
        local: {run_seconds: 60, heartbeat_seconds: 10}
      scheduler: {max_parallel_tasks: 2}
    graphs:
      main:
        max_supersteps: 5
        nodes:
          only: {uses: operation:no-op, retry: never, timeout: local}
        edges:
          - {from: START, to: only}
          - {from: only, to: END}
    gates: {}
    """
)


def test_assemble_graph_runtime_runs_minimal_workflow(tmp_path: Path) -> None:
    project = _prepare(tmp_path)
    contracts = parse_execution_contracts(_MINIMAL_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_MINIMAL_WORKFLOW), contracts)
    change_dir = project / "qa" / "changes" / "CH-1"

    runtime = assemble_graph_runtime(
        project_root=project,
        change_dir=change_dir,
        compiled=compiled,
        contracts=contracts,
        adapter=NeverCalledInvoker(),
    )
    result = runtime.run(
        compiled,
        "full",
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change_dir,
            change_id="CH-1",
            params={},
        ),
    )

    assert result.exit_code == 0
    assert result.status.status == "completed"
