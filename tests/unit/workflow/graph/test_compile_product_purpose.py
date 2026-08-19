import pytest

from assurance_agent.workflow.graph.capability_state import (
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.compiler import compile_loaded_workflow
from assurance_agent.workflow.graph.schema_v2 import LoadedWorkflowV2, parse_workflow_v2
from tests.helpers_aa import overlay_workflow_yaml

_MINIMAL = overlay_workflow_yaml(
    name="sample-product",
    entrypoint="ping",
    node="ping",
    uses="operation:no-op",
)


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
