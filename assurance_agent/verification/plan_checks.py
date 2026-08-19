from assurance_kernel.verification.plan_checks import *  # noqa: F403
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.verification.plan_checks")
