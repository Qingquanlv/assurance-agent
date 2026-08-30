from __future__ import annotations

import sys
from pathlib import Path

_RUNTIME_TESTS = Path(__file__).resolve().parent / "runtime"
if str(_RUNTIME_TESTS) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_TESTS))
