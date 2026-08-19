from pathlib import Path

from assurance_agent import resources


def test_kernel_policy_is_not_under_product_resources() -> None:
    product = Path("assurance_agent/_resources/schemas/policy-default.yaml")
    assert not product.is_file()
    text = resources.read_text("schemas", "policy-default.yaml")
    assert "policy" in text or len(text) > 0


def test_ingest_catalog_does_not_use_file_arithmetic() -> None:
    source = Path("assurance_agent/workflow/graph/ingest_catalog.py").read_text(encoding="utf-8")
    assert "_resources/schemas/ingest-artifact-catalog.yaml" not in source
    assert "parents[2]" not in source
