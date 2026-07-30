"""required_capabilities keys must resolve to real L1 leaves (spec C2b)."""

from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.capability_keys import check_capability_keys

DK = {
    "version": 1,
    "accounts": {},
    "auth": {"api_admin_token": {"method": "token"}},
    "entities": {},
    "capabilities": {
        "domain_factories": {},
        "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
        "cleanup": {},
    },
}


def _ctx(*, required: list[str] | None = None) -> CheckContext:
    return CheckContext(
        plan_texts={},
        cases=(),
        data_knowledge=DK,
        required_capabilities=tuple(required or ()),
    )


def test_empty_required_capabilities_passes() -> None:
    assert check_capability_keys(_ctx()).status == "pass"


def test_present_leaf_passes() -> None:
    assert check_capability_keys(_ctx(required=["auth.api_admin_token"])).status == "pass"


def test_missing_leaf_emits_locator_and_expected() -> None:
    result = check_capability_keys(_ctx(required=["auth.missing_token", "auth.api_admin_token"]))
    assert result.status == "fail"
    assert len(result.findings) == 1
    assert result.findings[0].locator == "required_capabilities[auth.missing_token]"
    assert result.findings[0].actual == "auth.missing_token"
    assert "data-knowledge" in result.findings[0].expected
