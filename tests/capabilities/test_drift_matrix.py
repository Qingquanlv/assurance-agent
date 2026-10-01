from __future__ import annotations

import pytest
from graph_engine.composition.source_fs import SourceSnapshotError

from tests.capabilities.drift_helpers import (
    assert_open_rejects_drift_without_ledger_append,
    resolve_with_one_mutation,
)

_FACETS = (
    "plugin-code",
    "plugin-version",
    "schema-bytes",
    "skill-bytes",
    "policy-bytes",
    "binding-data",
    "capability-catalog",
)


@pytest.mark.parametrize("facet", _FACETS)
def test_every_authority_facet_changes_composition_and_blocks_old_open(facet: str) -> None:
    original, drifted = resolve_with_one_mutation(facet)
    assert original.invocation_lock.canonical_bytes() != drifted.invocation_lock.canonical_bytes()
    assert_open_rejects_drift_without_ledger_append(original, drifted)


def test_result_contract_model_change_requires_a_regenerated_declaration() -> None:
    with pytest.raises(SourceSnapshotError, match="descriptor disagrees with static declaration"):
        resolve_with_one_mutation("result-contract-model")
