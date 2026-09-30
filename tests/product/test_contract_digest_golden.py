from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts
from graph_engine.canonical import JSONValue, canonical_digest

GOLDEN = Path(__file__).resolve().parent / "goldens" / "contract-digests.json"


def current_contract_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for contract in all_feature_agent_contracts().values():
        projection = cast(JSONValue, contract.canonical_projection())
        digests[f"agent:{contract.contract_id}"] = canonical_digest(projection)
    for contract in all_feature_task_contracts().values():
        projection = cast(JSONValue, contract.canonical_projection())
        digests[f"task:{contract.contract_id}"] = canonical_digest(projection)
    return dict(sorted(digests.items()))


def test_contract_digests_match_golden() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current_contract_digests() == expected
