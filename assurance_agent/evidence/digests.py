from assurance_kernel.evidence.digests import *  # noqa: F403
from assurance_kernel.evidence.digests import (  # noqa: F401
    _REDACTED as _REDACTED,
    _REDACT_PATTERNS as _REDACT_PATTERNS,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.evidence.digests")
