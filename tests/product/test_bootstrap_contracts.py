from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_product.bootstrap.contracts import (
    BootstrapStatusV1,
    RouteDefaultsV1,
    RunSpecV1,
    SutEndpointV1,
)


def _sut(**overrides: object) -> SutEndpointV1:
    payload: dict[str, object] = {
        "base_url": "http://127.0.0.1:9999",
        "readiness_url": "http://127.0.0.1:9999/openapi.json",
        "env": {"BASE_URL": "http://127.0.0.1:9999"},
        "env_from_node": ("QA_ADMIN_PASSWORD",),
    }
    payload.update(overrides)
    return SutEndpointV1.model_validate(payload)


def _spec(**overrides: object) -> RunSpecV1:
    payload: dict[str, object] = {
        "schema_version": "1",
        "product": "assurance-opencode",
        "entrypoint": "full",
        "requirement": "Cover dept CRUD on /api/v1/dept.",
        "candidate_test_families": ["api"],
        "case_modules": ["system/dept"],
        "sut": _sut().model_dump(mode="json"),
        "routes": {"provider_model": "deepseek/deepseek-v4-flash", "worker_profile": "max"},
        "budgets": {
            "review_rounds": 4,
            "coverage_rounds": 2,
            "healing_rounds": 2,
            "execution_retries": 2,
        },
        "opencode_token_env": "AA_NEXT_OPENCODE_TOKEN",
        "timeout_seconds": 28800,
    }
    payload.update(overrides)
    return RunSpecV1.model_validate(payload)


def test_valid_spec_round_trips() -> None:
    spec = _spec()
    dumped = spec.model_dump(mode="json")
    assert dumped["product"] == "assurance-opencode"
    assert dumped["entrypoint"] == "full"
    assert dumped["candidate_test_families"] == ["api"]
    assert dumped["case_modules"] == ["system/dept"]
    assert dumped["sut"]["env_from_node"] == ["QA_ADMIN_PASSWORD"]
    assert RunSpecV1.model_validate(dumped) == spec


def test_bootstrap_derives_only_the_http_origin_from_sut_base_url() -> None:
    from assurance_product.bootstrap.composition import _origin_from_base_url

    assert _origin_from_base_url("https://sut.example:8443/api/v1") == "https://sut.example:8443"
    with pytest.raises(ValueError, match="without credentials"):
        _origin_from_base_url("https://user:secret@sut.example/api")


def test_spec_rejects_unknown_product() -> None:
    with pytest.raises(ValidationError):
        _spec(product="other-product")


def test_spec_rejects_secret_values_in_env_from_node() -> None:
    with pytest.raises(ValidationError):
        _sut(env_from_node=("QA_ADMIN_PASSWORD=hunter2",))


def test_spec_rejects_duplicate_families() -> None:
    with pytest.raises(ValidationError):
        _spec(candidate_test_families=["api", "api"])


def test_status_defaults() -> None:
    status = BootstrapStatusV1(phase="preparing", change_id="BOOT-1")
    dumped = status.model_dump(mode="json")
    assert dumped["phase"] == "preparing"
    assert dumped["opencode"] is None
    assert dumped["status"] == {}
    assert dumped["error"] is None
    assert dumped["exit_code"] is None


def test_status_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BootstrapStatusV1.model_validate({"phase": "preparing", "change_id": "BOOT-1", "token": "secret"})


def test_route_defaults_are_exact_tokens() -> None:
    routes = RouteDefaultsV1(provider_model="deepseek/deepseek-v4-flash", worker_profile="max")
    assert routes.provider_model == "deepseek/deepseek-v4-flash"
    with pytest.raises(ValidationError):
        RouteDefaultsV1(provider_model="a,b", worker_profile="max")


def test_spec_allows_empty_case_modules() -> None:
    spec = _spec(case_modules=[])
    assert spec.case_modules == ()


def test_spec_still_rejects_unsafe_case_modules() -> None:
    with pytest.raises(ValidationError):
        _spec(case_modules=["../escape"])
