from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
from assurance_product.bootstrap.composition import PREPARE_IDS, prepare_composition
from assurance_product.models import ProductInputV1
from tests.product.conformance import PREPARE_IDS as CONFORMANCE_PREPARE_IDS
from tests.product.test_bootstrap_contracts import _spec

pytestmark = pytest.mark.usefixtures("installed_sources")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "sut"
    aa = project / ".aa"
    aa.mkdir(parents=True)
    (aa / "policy.yaml").write_text(
        "schema_version: '1'\norganization: example\n",
        encoding="utf-8",
    )
    (aa / "data-knowledge.yaml").write_text("version: 1\nnotes: []\n", encoding="utf-8")
    return project


def test_prepare_ids_match_conformance() -> None:
    assert tuple(PREPARE_IDS) == CONFORMANCE_PREPARE_IDS
    assert set(PREPARE_IDS) == set(AGENT_EXECUTION_CONTRACTS)


def test_prepare_composition_writes_authenticated_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    run_dir = tmp_path / "run"
    prepared = prepare_composition(
        project_dir=project,
        run_dir=run_dir,
        spec=_spec(),
        opencode_endpoint="http://127.0.0.1:4101",
        change_id="BOOT-1",
    )
    assert prepared["product"] == "assurance-opencode"
    input_path = Path(str(prepared["input_path"]))
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    value = ProductInputV1.model_validate(payload)
    assert value.change_id == "BOOT-1"
    catalog_bytes = (project / ".aa" / "capability-catalog.json").read_bytes()
    assert value.capability_catalog.sha256 == hashlib.sha256(catalog_bytes).hexdigest()
    deployment = yaml.safe_load((run_dir / "deployment.yaml").read_text(encoding="utf-8"))
    assert deployment["adapter_binding"]["endpoint"] == "http://127.0.0.1:4101"


def test_prepare_composition_rejects_missing_policy(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".aa" / "policy.yaml").unlink()
    with pytest.raises(ValueError, match="policy"):
        prepare_composition(
            project_dir=project,
            run_dir=tmp_path / "run",
            spec=_spec(),
            opencode_endpoint="http://127.0.0.1:4101",
            change_id="BOOT-1",
        )
