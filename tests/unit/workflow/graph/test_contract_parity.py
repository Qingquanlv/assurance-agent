"""Catalog ↔ execution-contracts structural parity (CI gate)."""

from __future__ import annotations

import yaml

from assurance_agent import resources
from assurance_agent.workflow.driver.operations_catalog import default_operations

# Documented compatibility alias: registered for older unit tests, not in contracts.
_CODE_ONLY_ALLOWLIST = frozenset({"operation:retro-accept"})


def _operation_contract_targets() -> set[str]:
    doc = yaml.safe_load(resources.read_text("schemas", "execution-contracts.yaml"))
    assert isinstance(doc, dict)
    contracts = doc.get("contracts")
    assert isinstance(contracts, dict)
    return {
        str(key)
        for key, entry in contracts.items()
        if isinstance(entry, dict) and entry.get("handler") == "operation"
    }


def test_operation_catalog_matches_execution_contracts() -> None:
    code_targets = set(default_operations())
    contract_targets = _operation_contract_targets()

    missing_contracts = sorted(code_targets - contract_targets - _CODE_ONLY_ALLOWLIST)
    missing_code = sorted(contract_targets - code_targets)
    allowlist_absent = sorted(_CODE_ONLY_ALLOWLIST - code_targets)

    assert not missing_contracts, (
        "operations registered in code but missing from execution-contracts.yaml: "
        + ", ".join(missing_contracts)
    )
    assert not missing_code, (
        "operations declared in execution-contracts.yaml but missing from catalog: " + ", ".join(missing_code)
    )
    assert not allowlist_absent, "code-only allowlist entries not present in catalog: " + ", ".join(
        allowlist_absent
    )
