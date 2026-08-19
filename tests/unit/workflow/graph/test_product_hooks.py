from pathlib import Path

import pytest

from assurance_agent.product import install_product, reset_product, select_product
from assurance_agent.workflow.graph.product_hooks import (
    ProductHooksMissing,
    current_product_hooks,
    reset_product_hooks,
)


def test_missing_hooks_fail_closed(tmp_path: Path) -> None:
    reset_product_hooks()
    with pytest.raises(ProductHooksMissing, match="load_product_code_roots"):
        current_product_hooks().load_product_code_roots(tmp_path)


def test_select_assurance_installs_product_code_roots_hook(tmp_path: Path) -> None:
    reset_product()
    select_product("assurance")
    roots = current_product_hooks().load_product_code_roots(tmp_path)
    assert isinstance(roots, list)


def test_missing_retro_hooks_fail_closed(tmp_path: Path) -> None:
    reset_product_hooks()
    with pytest.raises(ProductHooksMissing, match="complete_signal_outputs"):
        current_product_hooks().complete_signal_outputs(tmp_path, ())
    with pytest.raises(ProductHooksMissing, match="complete_candidate_outputs"):
        current_product_hooks().complete_candidate_outputs(tmp_path, ())


def test_select_assurance_installs_retro_output_hooks(tmp_path: Path) -> None:
    reset_product()
    select_product("assurance")
    hooks = current_product_hooks()
    hooks.complete_signal_outputs(tmp_path, ())
    hooks.complete_candidate_outputs(tmp_path, ())


def test_install_product_does_not_treat_assurance_id_as_special() -> None:
    """Kernel must not fill hooks by id. A dummy with id=assurance and no product_hooks stays fail-closed."""

    class Dummy:
        id = "assurance"

        def resource_root(self):
            from importlib.resources import files

            return files("assurance_agent") / "_resources"

        def register(self):
            from assurance_agent.workflow.graph.capability_state import CapabilityCatalog
            from assurance_agent.workflow.graph.handlers.operation import stop_operation

            builder = CapabilityCatalog()
            builder.register_operation("operation:stop")
            return builder.freeze(), {"operation:stop": stop_operation}, ()

    reset_product()
    install_product(Dummy())
    with pytest.raises(ProductHooksMissing):
        current_product_hooks().load_product_code_roots(Path("."))
