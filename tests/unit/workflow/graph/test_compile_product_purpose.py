import pytest

from assurance_agent.workflow.graph.capability_state import (
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.compiler import compile_loaded_workflow
from assurance_agent.workflow.graph.schema_v2 import LoadedWorkflowV2, parse_workflow_v2

_MINIMAL = """\
name: sample-product
entrypoints:
  ping: {graph: main, restart: repeatable}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 10}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 4
    nodes:
      ping:
        uses: operation:no-op
        retry: never
        timeout: local
    edges:
      - {from: START, to: ping}
      - {from: ping, to: END}
gates: {}
"""


def test_packaged_origin_non_assurance_skips_four_layer_gates() -> None:
    reset_current_product_id()
    install_current_product_id("sample")
    loaded = LoadedWorkflowV2(schema=parse_workflow_v2(_MINIMAL), origin="packaged")
    compiled = compile_loaded_workflow(loaded, contracts=None)
    assert "ping" in compiled.schema.entrypoints
    reset_current_product_id()


def test_packaged_origin_assurance_still_requires_four_layer() -> None:
    from assurance_agent.workflow.graph.compiler import CompileError

    reset_current_product_id()
    loaded = LoadedWorkflowV2(schema=parse_workflow_v2(_MINIMAL), origin="packaged")
    with pytest.raises(CompileError, match="(?i)activation|packaged"):
        compile_loaded_workflow(loaded, contracts=None)
