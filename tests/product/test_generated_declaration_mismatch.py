from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

import pytest

from tests.product.composition_harness import evict_generated_binding_modules

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deployment"
_OPENCODE_MANIFEST = _FIXTURE_DIR / "opencode.yaml"


@pytest.fixture
def opencode_manifest() -> Path:
    return _OPENCODE_MANIFEST


def _install_wheel(wheel: Path, dest: Path) -> Path:
    dest.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(dest)
    return dest


def test_generated_provider_rejects_declaration_contribution_mismatch(
    tmp_path: Path, opencode_manifest: Path
) -> None:
    from graph_engine.plugin_api import PluginContractError, RegistryPorts

    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    installed = _install_wheel(built.wheel, tmp_path / "installed")
    declaration_path = installed / built.import_package / "assurance-deployment-plugin.json"
    document = json.loads(declaration_path.read_text(encoding="utf-8"))
    bindings = document["descriptor"]["bindings"]
    assert isinstance(bindings, list) and bindings
    bindings.pop()
    declaration_path.write_text(json.dumps(document), encoding="utf-8")

    evict_generated_binding_modules()
    sys.path.insert(0, str(installed))
    try:
        module = __import__(f"{built.import_package}.provider", fromlist=["DeploymentPlugin"])
        with pytest.raises(PluginContractError, match="binding declarations disagree"):
            module.DeploymentPlugin.contribute(RegistryPorts("2.0"))
    finally:
        evict_generated_binding_modules()
        sys.path.remove(str(installed))
