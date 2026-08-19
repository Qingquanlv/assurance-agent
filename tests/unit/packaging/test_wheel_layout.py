import ast
import importlib
from pathlib import Path

KERNEL = Path("packages/assurance-kernel/assurance_kernel")


def test_kernel_tree_has_no_assurance_agent_imports() -> None:
    assert KERNEL.is_dir()
    banned: list[str] = []
    for path in KERNEL.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "assurance_agent" or alias.name.startswith("assurance_agent."):
                        banned.append(f"{path}:{alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "assurance_agent" or node.module.startswith("assurance_agent."):
                    banned.append(f"{path}:{node.module}")
    assert banned == []


def test_kernel_leaf_shims_are_module_aliases() -> None:
    for src in KERNEL.rglob("*.py"):
        if src.name in {"__init__.py", "__main__.py"}:
            continue
        rel = ".".join(src.relative_to(KERNEL).with_suffix("").parts)
        if rel == "workflow.driver.operations_catalog":
            continue
        old = importlib.import_module(f"assurance_agent.{rel}")
        new = importlib.import_module(f"assurance_kernel.{rel}")
        assert old is new, rel


def test_graph_package_reexports_graph_runtime() -> None:
    from assurance_agent.workflow.graph import GraphRuntime

    assert GraphRuntime.__module__.startswith("assurance_kernel.")


def test_load_product_assurance_still_works() -> None:
    from assurance_agent.product import load_product

    product = load_product("assurance")
    assert product.id == "assurance"
    assert type(product).__module__ == "assurance_agent.assurance_product"


def test_operations_catalog_stays_on_assurance_agent() -> None:
    import assurance_agent.workflow.driver.operations_catalog as oc

    assert oc.__file__ is not None
    parts = Path(oc.__file__).parts
    assert "assurance_agent" in parts
    assert "assurance-kernel" not in parts


def test_kernel_capability_catalog_has_no_product_catalog_edge() -> None:
    text = (KERNEL / "workflow/driver/capability_catalog.py").read_text(encoding="utf-8")
    assert "operations_catalog" not in text
    assert "def build_default_catalog" not in text
    assert "default_operations" not in text


def test_product_entry_group_literal_is_not_rewritten() -> None:
    text = (KERNEL / "product.py").read_text(encoding="utf-8")
    assert 'PRODUCT_ENTRY_GROUP = "assurance_agent.products"' in text
