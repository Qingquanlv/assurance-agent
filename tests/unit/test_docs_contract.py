"""Documentation contract for four-layer assurance / v6 runtime evidence semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

DOCS = {
    "schemas": REPO / "docs" / "schemas.md",
    "eval": REPO / "docs" / "eval.md",
    "readme": REPO / "README.md",
    "release": REPO / "docs" / "release-notes" / "2026-08-four-layer-assurance.md",
}

SCHEMA_IDS = (
    "plan_gate_semantics/v1",
    "historical_topology_safety/v1",
    "runtime_commit_safety/v1",
    "selection_normalizer/v1",
    "write_policy/v1",
)

V6_FIELDS = (
    "gate_semantics_digest",
    "gate_semantics_object_id",
    "topology_safety_semantics_digest",
    "topology_safety_semantics_object_id",
    "commit_safety_semantics_digest",
    "commit_safety_semantics_object_id",
)

VALIDATOR_IDS = (
    "generated_files_candidate/v1",
    "codegen_fix_candidate/v1",
)

EFFECT_KINDS = (
    "healing_allocation/v2",
    "fixer_proposal_approved/v1",
    "heal_record_apply/v2",
)

HARD_METRICS = (
    "current_assurance_chain_rate",
    "current_codegen_attempt_rate",
    "selected_test_write_rate",
)

LAYERS = ("api", "e2e", "fuzz", "performance")

FORBIDDEN_PHRASES = (
    "single-layer codegen-only",
    "summary-based authority",
    "automatic imported-codegen healing",
)


def _read(name: str) -> str:
    path = DOCS[name]
    assert path.is_file(), f"missing documentation file: {path}"
    return path.read_text(encoding="utf-8")


def _corpus() -> str:
    return "\n".join(_read(name) for name in DOCS)


@pytest.mark.parametrize("name", sorted(DOCS))
def test_required_docs_exist(name: str) -> None:
    assert DOCS[name].is_file()


def test_exact_schema_ids_documented() -> None:
    corpus = _corpus()
    for schema_id in SCHEMA_IDS:
        assert schema_id in corpus, f"missing schema id {schema_id}"


def test_six_v6_semantic_fields_documented() -> None:
    corpus = _corpus()
    for field in V6_FIELDS:
        assert field in corpus, f"missing v6 semantic field {field}"


def test_both_validator_ids_documented() -> None:
    corpus = _corpus()
    for validator_id in VALIDATOR_IDS:
        assert validator_id in corpus, f"missing validator id {validator_id}"


def test_three_effect_kinds_documented() -> None:
    corpus = _corpus()
    for kind in EFFECT_KINDS:
        assert kind in corpus, f"missing durable effect kind {kind}"


def test_legacy_block_reason_and_supersede_exit_documented() -> None:
    corpus = _corpus()
    assert "legacy_commit_safety_semantics_unbound" in corpus
    assert "aa workflow supersede" in corpus
    assert "rerun-v6" in corpus
    assert "stop" in corpus
    assert "sole audited" in corpus.lower() or "only audited" in corpus.lower()


def test_layer_selection_default_and_values_documented() -> None:
    corpus = _corpus()
    for layer in LAYERS:
        assert f"`{layer}`" in corpus or f'"{layer}"' in corpus or f"'{layer}'" in corpus
    assert "api" in corpus and "e2e" in corpus
    assert "default" in corpus.lower()
    assert "selection_normalizer/v1" in corpus


def test_evidence_export_algorithm_documented() -> None:
    corpus = _corpus()
    assert "export_seq" in corpus
    assert "1..N" in corpus or "1…N" in corpus or "exactly 1" in corpus
    assert "source_seq" in corpus
    assert "evidence-export" in corpus or "evidence export" in corpus.lower()


def test_three_hard_metrics_documented() -> None:
    corpus = _corpus()
    for metric in HARD_METRICS:
        assert metric in corpus, f"missing hard metric {metric}"
    assert "1.0" in corpus
    assert "L2-" in corpus and "codegen-pending" in corpus


def test_compatibility_and_narrowing_documented() -> None:
    corpus = _corpus()
    assert "v1" in corpus and "v5" in corpus
    assert "parseable" in corpus.lower() or "displayable" in corpus.lower() or "readable" in corpus.lower()
    assert "topology" in corpus.lower()
    assert "commit-safety" in corpus or "commit safety" in corpus.lower()
    assert "report-only" in corpus.lower() or "report/terminal" in corpus.lower()
    assert "imported-codegen" in corpus.lower() or "imported codegen" in corpus.lower()
    assert "declared-only" in corpus.lower() or "declared_only" in corpus


def test_rejects_stale_authority_claims() -> None:
    corpus = _corpus().lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in corpus, f"stale/forbidden phrase present: {phrase}"
