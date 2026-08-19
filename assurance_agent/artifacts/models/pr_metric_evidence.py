from assurance_kernel.artifacts.models.pr_metric_evidence import *  # noqa: F403
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.artifacts.models.pr_metric_evidence")
