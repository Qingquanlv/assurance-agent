from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tests.phase5.conformance import PREPARE_IDS, load_yaml

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deployment"
_OPENCODE_MANIFEST = _FIXTURE_DIR / "opencode.yaml"


@pytest.fixture
def opencode_manifest() -> Path:
    return _OPENCODE_MANIFEST


@pytest.fixture
def opencode_document() -> dict[str, object]:
    return deepcopy(load_yaml(_OPENCODE_MANIFEST))


def _write_manifest(path: Path, document: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("field", ["python", "secret_value", "default_model", "fallback_endpoint"])
def test_manifest_rejects_executable_or_fallback_fields(opencode_document, field):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    opencode_document[field] = "forbidden"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DeploymentBindingsV1.model_validate(opencode_document)


@pytest.mark.parametrize("field", ["python", "secret_value", "default_model", "fallback_endpoint"])
def test_adapter_binding_rejects_executable_or_fallback_fields(opencode_document, field):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    adapter = dict(opencode_document["adapter_binding"])
    adapter[field] = "forbidden"
    opencode_document["adapter_binding"] = adapter
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_manifest_rejects_fallback_lists_in_provider_model(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    assignment = dict(routes[PREPARE_IDS[0]])
    assignment["provider_model"] = "primary,fallback"
    routes[PREPARE_IDS[0]] = assignment
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="fallback|routing|candidate"):
        DeploymentBindingsV1.model_validate(opencode_document)


@pytest.mark.parametrize(
    "value",
    ["{{model}}", "model*", "sk-secret-canary-value", "env:OPENCODE_TOKEN"],
)
def test_manifest_rejects_templates_globs_and_secret_values(opencode_document, value):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    assignment = dict(routes[PREPARE_IDS[0]])
    assignment["worker_profile"] = value
    routes[PREPARE_IDS[0]] = assignment
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_secret_handles_must_equal_adapter_handles(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    opencode_document["secret_handles"] = ["opencode.token", "extra.unused"]
    with pytest.raises(ValidationError, match="secret"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_secret_handle_rejects_embedded_values(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    adapter = dict(opencode_document["adapter_binding"])
    adapter["secret_handle"] = "sk-secret-canary-value"
    opencode_document["adapter_binding"] = adapter
    opencode_document["secret_handles"] = ["sk-secret-canary-value"]
    with pytest.raises(ValidationError):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_builder_rejects_zip_path_escape(tmp_path, opencode_document):
    from pydantic import ValidationError

    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel
    from assurance_product.models import DeploymentBindingsV1

    profiles = dict(opencode_document["permission_profiles"])
    profiles["../evil"] = profiles["assurance.product.agent.permission.default"]
    opencode_document["permission_profiles"] = profiles
    with pytest.raises((ValidationError, BindingBuildError), match="qualified|escape|permission"):
        DeploymentBindingsV1.model_validate(opencode_document)
    manifest = _write_manifest(tmp_path / "escape.yaml", opencode_document)
    with pytest.raises((ValidationError, BindingBuildError), match="qualified|escape|permission"):
        build_deployment_wheel(manifest, tmp_path / "out")


def test_builder_rejects_capability_source_drift(monkeypatch, opencode_manifest, tmp_path):
    from assurance_intake.plugin import IntakePlugin
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    original = IntakePlugin.descriptor

    def drifted() -> object:
        descriptor = original()
        handlers = tuple(
            handler for handler in descriptor.task_handlers if handler != "assurance.intake.intake.prepare"
        )
        return descriptor.model_copy(update={"task_handlers": handlers})

    monkeypatch.setattr(IntakePlugin, "descriptor", staticmethod(drifted))
    with pytest.raises(BindingBuildError, match="source drift"):
        build_deployment_wheel(opencode_manifest, tmp_path / "out")


def test_built_wheel_contains_no_secret_bytes(tmp_path, opencode_manifest):
    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    payload = built.wheel.read_bytes()
    for canary in (
        b"sk-secret-canary-value",
        b"secret_value",
        b"OPENCODE_TOKEN=",
        b"CURSOR_API_KEY=",
        b"Bearer ",
    ):
        assert canary not in payload
    with __import__("zipfile").ZipFile(built.wheel) as archive:
        for name in archive.namelist():
            assert ".." not in name.split("/")
            assert not name.startswith("/")
            content = archive.read(name)
            assert b"sk-secret-canary-value" not in content
            assert b"secret_value" not in content
