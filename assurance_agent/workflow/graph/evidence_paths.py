from assurance_kernel.workflow.graph.evidence_paths import *  # noqa: F403
from assurance_kernel.workflow.graph.evidence_paths import (  # noqa: F401
    _assign_ownership as _assign_ownership,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.evidence_paths")
