from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.graph_purity_inventory import scan_graph_purity, violation_counts


def test_graph_modules_have_no_impurity() -> None:
    found = scan_graph_purity()
    assert found == {}, "graph modules must not parse, touch the filesystem, or import domain:\n" + "\n".join(
        f"{path}: {kinds}" for path, kinds in sorted(found.items())
    )


def test_violation_counts_cover_each_kind() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import hashlib",
                "import json as json_mod",
                "from pathlib import Path",
                "import yaml",
                "from assurance_intake.domain.history_refs import merge_history_refs",
                "def read(path):",
                "    raw = open(path)",
                "    builtins.open(path)",
                "    path.read_bytes()",
                "    path.read_text()",
                "    path.write_bytes(b'')",
                "    path.write_text('')",
                "    Model.model_validate({})",
                "    Model.model_validate_json('{}')",
            ]
        )
    )
    assert violation_counts(tree) == {
        "call:model_validate": 2,
        "call:open": 2,
        "call:read_bytes": 1,
        "call:read_text": 1,
        "call:write_bytes": 1,
        "call:write_text": 1,
        "import:domain": 1,
        "import:hashlib": 1,
        "import:json": 1,
        "import:pathlib": 1,
        "import:yaml": 1,
    }


def test_scanner_scopes_capability_and_product_graph_modules(tmp_path: Path) -> None:
    graph = tmp_path / "packages/capabilities/assurance-demo/assurance_demo/graphs/nodes.py"
    nested = tmp_path / "packages/capabilities/assurance-demo/assurance_demo/graphs/extra/hidden.py"
    other = tmp_path / "packages/capabilities/assurance-demo/assurance_demo/ops/nodes.py"
    product = tmp_path / "packages/products/assurance-product/assurance_product/graphs/execute.py"
    for path, source in (
        (graph, "import json\n"),
        (nested, "import json\n"),
        (other, "import json\n"),
        (product, "from pathlib import Path\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    assert scan_graph_purity(tmp_path) == {
        "packages/capabilities/assurance-demo/assurance_demo/graphs/nodes.py": {"import:json": 1},
        "packages/products/assurance-product/assurance_product/graphs/execute.py": {"import:pathlib": 1},
    }
