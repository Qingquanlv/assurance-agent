"""Regenerate the frozen pre-modular Assurance Workflow golden.

The writer may write only these two repository fixtures:
- tests/product/goldens/assurance-full-pre-modular.json
- tests/product/fixtures/workflow-module-ownership.yaml

It regenerates the golden from ``load_pre_modular_workflow()``. The ownership
YAML is a reviewed inventory and is not generated here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from graph_engine.canonical import canonical_json_bytes

from assurance_product.product import load_pre_modular_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests/product/goldens/assurance-full-pre-modular.json"
FIXTURE_PATH = REPO_ROOT / "tests/product/fixtures/assurance-full-pre-modular.yaml"
OWNERSHIP_PATH = REPO_ROOT / "tests/product/fixtures/workflow-module-ownership.yaml"
ALLOWED_DESTINATIONS = frozenset({GOLDEN_PATH.resolve(), OWNERSHIP_PATH.resolve()})


def write_allowed(path: Path, content: bytes) -> None:
    resolved = path.resolve()
    if resolved not in ALLOWED_DESTINATIONS:
        raise SystemExit(f"refusing to write {path}: only {GOLDEN_PATH} and {OWNERSHIP_PATH} are allowed")
    if resolved == OWNERSHIP_PATH.resolve():
        raise SystemExit(
            f"refusing to write {path}: ownership YAML is a reviewed inventory, not a generated dump"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(content)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    destination = GOLDEN_PATH if not args else Path(args[0])
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).resolve()
    workflow = load_pre_modular_workflow(FIXTURE_PATH)
    payload = canonical_json_bytes(workflow.model_dump(mode="json", by_alias=True, exclude_unset=True))
    write_allowed(destination, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
