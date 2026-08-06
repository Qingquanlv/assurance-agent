from assurance_agent.evidence.case_id import canonicalize_case_id, extract_case_id


def test_canonicalize_uppercases_and_underscores() -> None:
    assert canonicalize_case_id("tc-menu-api-001") == "TC_MENU_API_001"
    assert canonicalize_case_id("TC_MENU_001") == "TC_MENU_001"


def test_extract_case_id_from_pytest_nodeid() -> None:
    nodeid = "tests/api/test_menu.py::test_tc_menu_api_001__create_menu_happy_path"
    assert extract_case_id(nodeid) == "TC_MENU_API_001"


def test_extract_case_id_accepts_legacy_hyphen_form() -> None:
    assert extract_case_id("TC-ROLE-002 role list") == "TC_ROLE_002"


def test_extract_case_id_is_case_insensitive() -> None:
    assert extract_case_id("tc_dept_001 department smoke") == "TC_DEPT_001"


def test_extract_case_id_returns_empty_when_absent() -> None:
    assert extract_case_id("test_plain_smoke_check") == ""


def test_extract_case_id_lookbehind_boundary_rejects_embedded_match() -> None:
    # A preceding alphanumeric char (no separator) must not be treated as a
    # boundary; the id must start right after a non [A-Z0-9] character.
    assert extract_case_id("XTC_MENU_001") == ""
