from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument


def test_ui_document_accepts_three_statuses() -> None:
    document = UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "live",
            "base_url": "http://127.0.0.1:3100",
            "warnings": [],
            "features": [
                {
                    "name": "登录",
                    "status": "explored",
                    "use_cases_reached": 1,
                    "use_cases_total": 1,
                    "flows": ["GET /login"],
                    "pages": [{"path": "/login", "landed_path": "/"}],
                    "summary": "登录后进入首页",
                }
            ],
        }
    )
    assert document.features[0].pages[0].landed_path == "/"


def test_api_document_groups_operations_by_family() -> None:
    document = ApiDiscoveryDocument.model_validate(
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
    assert document.operation_keys() == {("POST", "/api/v1/user/create")}


def test_unavailable_documents_have_empty_collections() -> None:
    ui = UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "unavailable",
            "base_url": "http://127.0.0.1:3100",
            "warnings": ["ui base did not respond"],
            "features": [],
        }
    )
    api = ApiDiscoveryDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "unused",
            "base_url": "",
            "warnings": [],
            "families": [],
        }
    )
    assert ui.features == ()
    assert api.operation_keys() == set()
