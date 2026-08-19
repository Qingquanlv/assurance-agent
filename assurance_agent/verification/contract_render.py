from assurance_kernel.verification.contract_render import *  # noqa: F403
from assurance_kernel.verification.contract_render import (  # noqa: F401
    _ShapeRenderer as _ShapeRenderer,
    _render_model as _render_model,
)
from importlib import import_module
import sys

sys.modules[__name__] = import_module("assurance_kernel.verification.contract_render")
