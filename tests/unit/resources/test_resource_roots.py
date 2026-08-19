from pathlib import Path

import pytest

from assurance_agent import resources


def test_kernel_policy_is_not_under_product_resources() -> None:
    product = Path("assurance_agent/_resources/schemas/policy-default.yaml")
    assert not product.is_file()
    text = resources.read_text("schemas", "policy-default.yaml")
    assert "policy" in text or len(text) > 0


def test_ingest_catalog_does_not_use_file_arithmetic() -> None:
    source = Path("packages/assurance-kernel/assurance_kernel/workflow/graph/ingest_catalog.py").read_text(
        encoding="utf-8"
    )
    assert "_resources/schemas/ingest-artifact-catalog.yaml" not in source
    assert "parents[2]" not in source


def test_kernel_resources_do_not_load_assurance_agent_package() -> None:
    source = Path("packages/assurance-kernel/assurance_kernel/resources.py").read_text(encoding="utf-8")
    assert 'files("assurance_agent")' not in source
    assert "import assurance_agent" not in source


def test_product_files_fail_closed_without_product_root() -> None:
    from assurance_agent.product import reset_product

    reset_product()
    with pytest.raises(FileNotFoundError, match="product resource root is not installed"):
        resources.read_text("schemas", "workflow-schema.yaml")
