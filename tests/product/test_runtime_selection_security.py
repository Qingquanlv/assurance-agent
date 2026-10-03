from __future__ import annotations


def test_cutover_validator_and_runtime_selector_are_gone() -> None:
    import importlib.util

    from assurance_product import application
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    assert importlib.util.find_spec("assurance_product.runtime_selection") is None
    assert not hasattr(application, "select_runtime")
    assert not hasattr(application, "use_test_runtime_selector")
    assert not hasattr(application, "validate_entrypoint_runtime_cutover")
    assert set(PRODUCT_ENTRYPOINTS) == set(application.ENTRYPOINT_AGENT_CONTRACT_IDS)
