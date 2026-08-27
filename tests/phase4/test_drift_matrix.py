from __future__ import annotations

import pytest

from tests.phase4.drift_helpers import (
    assert_open_rejects_drift_without_ledger_append,
    resolve_with_one_mutation,
)

_FACETS = (
    "plugin-code",
    "plugin-version",
    "schema-bytes",
    "skill-bytes",
    "persona-bytes",
    "result-contract-bytes",
    "policy-bytes",
    "binding-data",
    "capability-catalog",
)


@pytest.mark.parametrize("facet", _FACETS)
def test_every_authority_facet_changes_composition_and_blocks_old_open(facet: str) -> None:
    original, drifted = resolve_with_one_mutation(facet)
    assert original.invocation_lock.canonical_bytes() != drifted.invocation_lock.canonical_bytes()
    assert_open_rejects_drift_without_ledger_append(original, drifted)
