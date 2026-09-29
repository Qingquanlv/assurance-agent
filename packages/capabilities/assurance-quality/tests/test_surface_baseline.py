import json

from assurance_quality.operations.surface_baseline import SurfaceProbeRequest, collect_surface


def test_openapi_probe_groups_user_create() -> None:
    spec = {
        "paths": {
            "/api/v1/user/create": {
                "post": {
                    "parameters": [{"name": "token", "in": "header", "required": True}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username"],
                                    "properties": {"username": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "integer"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }

    def fetch(url: str) -> tuple[int, bytes, str]:
        assert url == "http://127.0.0.1:9999/openapi.json"
        return 200, json.dumps(spec).encode(), url

    result = collect_surface(
        SurfaceProbeRequest(
            change_id="CH-1",
            api_base_url="http://127.0.0.1:9999",
            ui_base_url=None,
            ui_paths=(),
            needs_api=True,
            needs_ui=False,
        ),
        fetch,
    )
    assert result.api.source == "live"
    assert result.api.operation_keys() == {("POST", "/api/v1/user/create")}
    assert result.api.families[0].name == "user"
    assert result.api.families[0].auth == "bearer"
    assert result.ui.source == "unused"


def test_ui_redirect_is_partial_and_connection_loss_is_unavailable() -> None:
    def fetch(url: str) -> tuple[int, bytes, str]:
        if url.endswith("/login"):
            return 302, b"", "http://127.0.0.1:3100/"
        raise OSError("connection refused")

    reached = collect_surface(
        SurfaceProbeRequest(
            change_id="CH-1",
            api_base_url=None,
            ui_base_url="http://127.0.0.1:3100",
            ui_paths=("/login",),
            needs_api=False,
            needs_ui=True,
        ),
        fetch,
    )
    assert reached.ui.features[0].status == "partial"
    assert reached.ui.features[0].pages[0].landed_path == "/"

    failed = collect_surface(
        SurfaceProbeRequest(
            change_id="CH-1",
            api_base_url="http://127.0.0.1:9999",
            ui_base_url=None,
            ui_paths=(),
            needs_api=True,
            needs_ui=False,
        ),
        fetch,
    )
    assert failed.api.source == "unavailable"
    assert failed.api.warnings
