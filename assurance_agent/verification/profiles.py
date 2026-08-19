from assurance_kernel.verification.profiles import *  # noqa: F403
from assurance_kernel.verification.profiles import (  # noqa: F401
    _build_profile_registry as _build_profile_registry,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.verification.profiles")
