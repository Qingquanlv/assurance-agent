from __future__ import annotations

import sys
from pathlib import Path

from assurance_generation.contracts.families import GENERATION_FAMILIES

_FEATURE_TESTS = Path(__file__).resolve().parents[2] / "packages/features/assurance-generation/tests"
if str(_FEATURE_TESTS) not in sys.path:
    sys.path.insert(0, str(_FEATURE_TESTS))

from test_workflow_module import _drive_generate  # noqa: E402


def test_all_family_join_is_order_independent() -> None:
    forward = _drive_generate(selected=GENERATION_FAMILIES)
    reverse = _drive_generate(selected=GENERATION_FAMILIES)
    assert forward.join_token_count == reverse.join_token_count == 4
    assert forward.dispatched_families == reverse.dispatched_families == set(GENERATION_FAMILIES)
    assert forward.skip_families == reverse.skip_families == set()
    assert forward.counters == reverse.counters
