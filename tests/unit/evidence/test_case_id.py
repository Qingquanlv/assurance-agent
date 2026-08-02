"""case_id extraction lives in evidence/ (sunk from workflow.execution)."""

from assurance_agent.evidence.case_id import canonicalize_case_id, extract_case_id


def test_canonicalize_uppercases_and_underscores() -> None:
    assert canonicalize_case_id("tc-menu-api-001") == "TC_MENU_API_001"
    assert canonicalize_case_id("TC_MENU_001") == "TC_MENU_001"


def test_extract_case_id_from_pytest_nodeid() -> None:
    nodeid = "tests/api/test_menu.py::test_tc_menu_api_001__create_menu_happy_path"
    assert extract_case_id(nodeid) == "TC_MENU_API_001"


def test_extract_case_id_accepts_legacy_hyphen_form() -> None:
    assert extract_case_id("TC-ROLE-002 role list") == "TC_ROLE_002"


def test_extract_case_id_returns_empty_when_absent() -> None:
    assert extract_case_id("test_plain_smoke_check") == ""


def test_lookbehind_rejects_prefix_glued_to_tc() -> None:
    assert extract_case_id("XTC_DEPT_API_001") == ""
    assert extract_case_id("test_xtc_dept_api_001__x") == ""
