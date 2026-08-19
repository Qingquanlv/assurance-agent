from assurance_kernel.workflow.graph.task_inputs import *  # noqa: F403
from assurance_kernel.workflow.graph.task_inputs import (  # noqa: F401
    _entries_input_sha256 as _entries_input_sha256,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.task_inputs")
