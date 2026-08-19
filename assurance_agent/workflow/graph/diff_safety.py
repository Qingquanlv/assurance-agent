from assurance_kernel.workflow.graph.diff_safety import *  # noqa: F403
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.diff_safety")
