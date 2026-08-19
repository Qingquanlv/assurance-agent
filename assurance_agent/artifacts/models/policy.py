from assurance_kernel.artifacts.models.policy import *  # noqa: F403
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.artifacts.models.policy")
