from graph_engine.attempts.host_protocol import *  # noqa: F403
from graph_engine.attempts import host_protocol as _impl
import sys

sys.modules[__name__] = _impl
