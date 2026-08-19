from assurance_kernel.artifacts.models.trace import *  # noqa: F403
from assurance_kernel.artifacts.models.trace import (  # noqa: F401
    StrictNonNegativeInt as StrictNonNegativeInt,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.artifacts.models.trace")
