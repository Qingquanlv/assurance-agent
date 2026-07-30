"""IngestArtifactCatalog：pinned model_schema_digest + drift fail-closed（P2）。"""

from __future__ import annotations

import pytest

from assurance_agent.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    load_ingest_catalog,
    model_schema_digest,
    validate_catalog_runtime,
)


def test_packaged_catalog_pins_real_model_schema_digest() -> None:
    catalog = load_ingest_catalog()
    expected = model_schema_digest("review@1")
    for symbol in ("api_plan_review", "e2e_plan_review"):
        spec = catalog.artifacts[symbol]
        assert spec.model_schema_digest == expected, symbol


def test_e2e_plan_review_uses_the_canonical_shared_review_path() -> None:
    catalog = load_ingest_catalog()
    assert catalog.artifacts["e2e_plan_review"].path == "change:review/plan-review.json"


def test_validate_catalog_runtime_fills_and_verifies_digests() -> None:
    catalog = validate_catalog_runtime()
    expected = model_schema_digest("review@1")
    assert catalog.artifacts["api_plan_review"].model_schema_digest == expected
    # path_only artifacts have no model digest.
    assert catalog.artifacts["api_plan"].model_schema_digest == ""


def test_validate_catalog_runtime_rejects_pinned_digest_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = IngestArtifactCatalog.model_validate(
        {
            "schema_version": 1,
            "artifacts": {
                "api_plan_review": {
                    "kind": "file_ingest",
                    "model": "review@1",
                    "model_schema_digest": "deadbeef" * 8,
                    "path": "change:review/api-plan-review.json",
                    "codec": "json",
                    "cardinality": "one",
                    "compat": "must_compat",
                    "ingest": {"mode": "full"},
                }
            },
        }
    )
    monkeypatch.setattr(
        "assurance_agent.workflow.graph.ingest_catalog.load_ingest_catalog",
        lambda: stale,
    )
    with pytest.raises(ValueError, match="model_schema_digest drift"):
        validate_catalog_runtime()
