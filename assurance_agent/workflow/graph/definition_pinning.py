from assurance_kernel.workflow.graph.definition_pinning import *  # noqa: F403
from assurance_kernel.workflow.graph.definition_pinning import (  # noqa: F401
    _pinned_reason_for_compile_error as _pinned_reason_for_compile_error,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.definition_pinning")
