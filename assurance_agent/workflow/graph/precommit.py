from assurance_kernel.workflow.graph.precommit import *  # noqa: F403
from assurance_kernel.workflow.graph.precommit import (  # noqa: F401
    _bind_approval_to_snapshot_artifacts as _bind_approval_to_snapshot_artifacts,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.workflow.graph.precommit")
