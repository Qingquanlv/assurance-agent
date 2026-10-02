from __future__ import annotations

import pytest
from assurance_intake.contracts.cases import CaseEntryAuthoring, CaseYamlAuthoring
from assurance_intake.domain.case_checks import reject_endpoint_literals
from assurance_intake.domain.surface_guard import SurfaceMismatch, assert_cases_match_surface
from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument


def _api() -> ApiDiscoveryDocument:
    return ApiDiscoveryDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "live",
            "base_url": "http://127.0.0.1:9999",
            "warnings": [],
            "families": [
                {
                    "name": "user",
                    "auth": "bearer",
                    "operations": [
                        {
                            "method": "POST",
                            "path": "/api/v1/user/create",
                            "request": {
                                "required_headers": ["token"],
                                "query": [],
                                "body_fields": ["username"],
                            },
                            "response": {"status_codes": [200], "body_fields": ["id"]},
                        }
                    ],
                }
            ],
        }
    )


def _ui() -> UiExplorationDocument:
    return UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "live",
            "base_url": "http://127.0.0.1:3100",
            "warnings": [],
            "features": [
                {
                    "name": "login",
                    "status": "explored",
                    "use_cases_reached": 1,
                    "use_cases_total": 1,
                    "flows": ["GET /login"],
                    "pages": [{"path": "/login", "landed_path": "/login"}],
                    "summary": "login page reached",
                }
            ],
        }
    )


def _case_with_api_step(method: str, path: str) -> CaseYamlAuthoring:
    entry = CaseEntryAuthoring.model_construct(
        type="API",
        steps=[{"method": method, "path": path}],
    )
    return CaseYamlAuthoring.model_construct(added=[entry], modified=[])


def _case_with_e2e_path(path: str) -> CaseYamlAuthoring:
    entry = CaseEntryAuthoring.model_construct(
        type="E2E",
        steps=[{"path": path}],
    )
    return CaseYamlAuthoring.model_construct(added=[entry], modified=[])


def test_api_step_must_match_a_discovered_operation() -> None:
    with pytest.raises(SurfaceMismatch, match="POST /api/v1/user/missing"):
        assert_cases_match_surface(
            _case_with_api_step("POST", "/api/v1/user/missing"), _api(), _ui(), {"api"}
        )


def test_api_case_needs_a_structured_method_path_step() -> None:
    entry = CaseEntryAuthoring.model_construct(case_id="TC_USER_001", type="API", steps=["调用用户创建接口"])
    document = CaseYamlAuthoring.model_construct(added=[entry], modified=[])

    with pytest.raises(SurfaceMismatch, match=r"TC_USER_001: .*structured step"):
        assert_cases_match_surface(document, _api(), _ui(), {"api"})


def test_structured_api_step_passes_surface_and_endpoint_literal_checks() -> None:
    document = _case_with_api_step("POST", "/api/v1/user/create")

    assert_cases_match_surface(document, _api(), _ui(), {"api"})
    reject_endpoint_literals(document)


def test_e2e_page_must_be_explored_or_partial() -> None:
    with pytest.raises(SurfaceMismatch, match="/missing"):
        assert_cases_match_surface(_case_with_e2e_path("/missing"), _api(), _ui(), {"e2e"})
