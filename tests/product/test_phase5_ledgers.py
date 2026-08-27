from tests.product.conformance import ALL_BINDING_IDS, PREPARE_IDS, load_json, load_yaml


def test_phase5_ledgers_are_closed_and_exact(evidence_root):
    ownership = load_yaml(evidence_root / "ownership.yaml")
    bindings = load_json(evidence_root / "binding-coverage.json")
    comparisons = load_yaml(evidence_root / "comparison-dispositions.yaml")
    assert ownership["engine_api"] == "2.0"
    assert len(PREPARE_IDS) == 33
    assert len(ALL_BINDING_IDS) == 99
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert all(bindings[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    cases = comparisons["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 25
