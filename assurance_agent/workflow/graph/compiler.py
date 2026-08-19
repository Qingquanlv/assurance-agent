from assurance_kernel.workflow.graph.compiler import *  # noqa: F403
from assurance_kernel.workflow.graph.compiler import (  # noqa: F401
    _compile_with_catalog as _compile_with_catalog,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.compiler")
