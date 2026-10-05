from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from graph_engine.plugin_api import TaskHandler

from assurance_generation.operations.cycle import PublishGenerationCycleHandler
from assurance_generation.operations.init_runtime import InitTestRuntimeHandler
from assurance_generation.operations.resolve_inputs import ResolveGenerationInputsHandler


def generation_handlers() -> Mapping[str, TaskHandler]:
    from assurance_generation import ops

    return MappingProxyType(
        {
            **dict(ops.router.handlers(cast(TaskHandler, ops))),
            "assurance.generation.resolve-inputs.execute": ResolveGenerationInputsHandler(),
            "assurance.generation.publish-cycle.execute": PublishGenerationCycleHandler(),
            "assurance.generation.init-test-runtime.execute": InitTestRuntimeHandler(),
        }
    )


__all__ = ["generation_handlers"]
