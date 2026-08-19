from assurance_kernel.verification.checks.registry import *  # noqa: F403
from assurance_kernel.verification.checks.registry import (  # noqa: F401
    _run_profile_checks as _run_profile_checks,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.verification.checks.registry")
