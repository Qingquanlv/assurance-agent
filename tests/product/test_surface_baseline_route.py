from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assurance_product.graphs.full import route_surface
from assurance_quality.contracts.surface import (
    API_DISCOVERY_PATH,
    UI_EXPLORATION_PATH,
    ApiDiscoveryDocument,
    UiExplorationDocument,
)


def _write_doc(root: Path, relative: str, payload: dict[str, object]) -> dict[str, str]:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(data)
    return {"path": relative, "digest": hashlib.sha256(data).hexdigest()}


def _live_ui() -> dict[str, object]:
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
                    "pages": [{"path": "/login", "landed_path": "/"}],
                    "summary": "GET /login -> /",
                }
            ],
        }
    ).model_dump(mode="json")


def _unavailable_ui() -> dict[str, object]:
    return UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "unavailable",
            "base_url": "http://127.0.0.1:3100",
            "warnings": ["ui base did not respond"],
            "features": [],
        }
    ).model_dump(mode="json")


def _unused_ui() -> dict[str, object]:
    return UiExplorationDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "unused",
            "base_url": "",
            "warnings": [],
            "features": [],
        }
    ).model_dump(mode="json")


def _live_api() -> dict[str, object]:
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
                    "auth": "none",
                    "operations": [
                        {
                            "method": "GET",
                            "path": "/api/v1/user/list",
                            "request": {"required_headers": [], "query": [], "body_fields": []},
                            "response": {"status_codes": [200], "body_fields": []},
                        }
                    ],
                }
            ],
        }
    ).model_dump(mode="json")


def _unused_api() -> dict[str, object]:
    return ApiDiscoveryDocument.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "source": "unused",
            "base_url": "",
            "warnings": [],
            "families": [],
        }
    ).model_dump(mode="json")


def test_route_surface_requires_live_side_for_requested_families(tmp_path: Path) -> None:
    ui_ref = _write_doc(tmp_path, UI_EXPLORATION_PATH, _live_ui())
    api_ref = _write_doc(tmp_path, API_DISCOVERY_PATH, _unused_api())
    prepare_state = {
        "candidate_test_families": ["e2e"],
        "ui_exploration_ref": ui_ref,
        "api_discovery_ref": api_ref,
    }
    assert route_surface(prepare_state, project_root=tmp_path) == "prepare"

    unavailable_ref = _write_doc(tmp_path, UI_EXPLORATION_PATH, _unavailable_ui())
    blocked_state = {
        "candidate_test_families": ["e2e"],
        "ui_exploration_ref": unavailable_ref,
        "api_discovery_ref": api_ref,
    }
    assert route_surface(blocked_state, project_root=tmp_path) == "not-achieved"

    api_live = _write_doc(tmp_path, API_DISCOVERY_PATH, _live_api())
    ui_unused = _write_doc(tmp_path, UI_EXPLORATION_PATH, _unused_ui())
    api_state = {
        "candidate_test_families": ["api"],
        "ui_exploration_ref": ui_unused,
        "api_discovery_ref": api_live,
    }
    assert route_surface(api_state, project_root=tmp_path) == "prepare"


def test_route_surface_uses_state_sources_without_project_root() -> None:
    state = {
        "candidate_test_families": ["api"],
        "ui_exploration_ref": {
            "path": UI_EXPLORATION_PATH,
            "digest": "0" * 64,
        },
        "api_discovery_ref": {
            "path": API_DISCOVERY_PATH,
            "digest": "1" * 64,
        },
        "ui_exploration_source": "unused",
        "api_discovery_source": "live",
    }
    assert route_surface(state) == "prepare"

    refs_only = {
        "candidate_test_families": ["api"],
        "ui_exploration_ref": state["ui_exploration_ref"],
        "api_discovery_ref": state["api_discovery_ref"],
    }
    assert route_surface(refs_only) == "not-achieved"
