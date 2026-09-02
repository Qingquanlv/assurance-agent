from graph_engine.attempts.host_receipts import *  # noqa: F403
from graph_engine.attempts import host_receipts as _impl
import sys

sys.modules[__name__] = _impl
