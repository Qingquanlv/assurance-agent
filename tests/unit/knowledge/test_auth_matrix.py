"""Auth matrix model + list_auth_routes capability contract (spec §5-A3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.data_knowledge import (
    AuthMatrixCell,
    DataKnowledge,
    DataKnowledgeProposal,
)
from assurance_agent.knowledge.capabilities import (
    is_leaf_present,
    validate_list_auth_routes_contract,
)
from assurance_agent.knowledge.extract_constraints import auth_matrix_known_keys


def _cell(**overrides: object) -> dict:
    cell: dict = {
        "route": "/api/v1/api/list",
        "method": "GET",
        "token": "api_admin_token",
        "expected": "allow",
        "allowed_status_codes": [200],
    }
    cell.update(overrides)
    return cell


def test_auth_matrix_cell_round_trip() -> None:
    cell = AuthMatrixCell.model_validate(_cell(expected="deny", allowed_status_codes=[401, 403]))
    assert cell.route == "/api/v1/api/list"
    assert cell.method == "GET"
    assert cell.token == "api_admin_token"
    assert cell.expected == "deny"
    assert cell.allowed_status_codes == [401, 403]


def test_data_knowledge_rejects_unknown_auth_matrix_token() -> None:
    with pytest.raises(ValidationError, match="unknown auth key"):
        DataKnowledge.model_validate(
            {
                "version": 1,
                "auth": {"api_admin_token": {"method": "token"}},
                "auth_matrix": {
                    "api_list_ghost": _cell(token="ghost_token"),
                },
            }
        )


def test_data_knowledge_accepts_declared_auth_matrix_token() -> None:
    dk = DataKnowledge.model_validate(
        {
            "version": 1,
            "auth": {"api_admin_token": {"method": "token"}},
            "auth_matrix": {"api_list_admin": _cell()},
        }
    )
    assert dk.auth_matrix["api_list_admin"].expected == "allow"


def test_proposal_rejects_unknown_auth_matrix_token() -> None:
    with pytest.raises(ValidationError, match="unknown auth key"):
        DataKnowledgeProposal.model_validate(
            {
                "schema_version": "1",
                "mode": "delta",
                "auth": {"api_admin_token": {"method": "token"}},
                "auth_matrix": {"cell": _cell(token="missing")},
            }
        )


def test_list_auth_routes_absent_is_ok() -> None:
    dk = DataKnowledge.model_validate({"version": 1})
    validate_list_auth_routes_contract(dk)


def test_list_auth_routes_contract_requires_async_factory_and_symbol() -> None:
    bad = {
        "domain_factories": {
            "api": {
                "list_auth_routes": {
                    "kind": "helper",
                    "symbol": "tests.testdata.domain.api.list_auth_routes",
                }
            }
        }
    }
    with pytest.raises(ValueError, match="list_auth_routes"):
        validate_list_auth_routes_contract({"capabilities": bad})


def test_list_auth_routes_valid_contract_passes() -> None:
    dk = DataKnowledge.model_validate(
        {
            "version": 1,
            "capabilities": {
                "domain_factories": {
                    "api": {
                        "list_auth_routes": {
                            "kind": "async_factory",
                            "symbol": "tests.testdata.domain.api.list_auth_routes",
                            "notes": "Reads app.routes with DependPermission",
                        }
                    }
                }
            },
        }
    )
    validate_list_auth_routes_contract(dk)


def test_data_knowledge_model_validates_list_auth_routes_when_present() -> None:
    with pytest.raises(ValidationError, match="list_auth_routes"):
        DataKnowledge.model_validate(
            {
                "version": 1,
                "capabilities": {
                    "domain_factories": {
                        "api": {
                            "list_auth_routes": {
                                "kind": "http",
                                "symbol": "tests.other.list_auth_routes",
                            }
                        }
                    }
                },
            }
        )


def test_auth_matrix_rejects_conflicting_duplicate_identity() -> None:
    with pytest.raises(ValidationError, match="route.*method.*token|duplicate"):
        DataKnowledge.model_validate(
            {
                "version": 1,
                "auth": {"api_admin_token": {"method": "token"}},
                "auth_matrix": {
                    "cell_a": _cell(expected="allow", allowed_status_codes=[200]),
                    "cell_b": _cell(expected="deny", allowed_status_codes=[401, 403]),
                },
            }
        )


def test_auth_matrix_known_keys_lists_cell_ids() -> None:
    dk = DataKnowledge.model_validate(
        {
            "version": 1,
            "auth": {"api_admin_token": {"method": "token"}},
            "auth_matrix": {"api_list_admin": _cell()},
        }
    )
    assert auth_matrix_known_keys(dk) == frozenset({"auth_matrix.api_list_admin"})


def test_list_auth_routes_rejects_suffixed_lookalike_symbol() -> None:
    with pytest.raises(ValidationError, match="list_auth_routes"):
        DataKnowledge.model_validate(
            {
                "version": 1,
                "capabilities": {
                    "domain_factories": {
                        "api": {
                            "list_auth_routes": {
                                "kind": "async_factory",
                                "symbol": "tests.testdata.domain.api.evil_list_auth_routes",
                            }
                        }
                    }
                },
            }
        )


def test_is_leaf_present_accepts_auth_matrix_cell() -> None:
    dk = {
        "version": 1,
        "auth": {"api_admin_token": {"method": "token"}},
        "auth_matrix": {"api_list_admin": _cell()},
        "entities": {},
        "capabilities": {
            "domain_factories": {},
            "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
            "cleanup": {},
        },
    }
    assert is_leaf_present(dk, "auth_matrix.api_list_admin") is True
    assert is_leaf_present(dk, "auth_matrix.missing") is False
