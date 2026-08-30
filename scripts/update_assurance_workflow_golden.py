"""Regenerate the frozen modular public-closure traces.

Writes only:
- tests/product/goldens/public-closure.json

Run from the repo root after an intentional public-behavior change, then review
the diff in the commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests/product/goldens/public-closure.json"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    destination = GOLDEN_PATH if not args else Path(args[0])
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).resolve()
    if destination.resolve() != GOLDEN_PATH.resolve():
        raise SystemExit(f"refusing to write {destination}: only {GOLDEN_PATH} is allowed")

    sys.path.insert(0, str(REPO_ROOT))
    from tests.product.composition_harness import build_installed_sources
    from tests.product.product_runner import modular_product_composition
    from tests.product.public_closure import record_public_closure_traces

    with TemporaryDirectory(prefix="public-closure-") as tmp:
        root = Path(tmp)
        sources_root = root / "sources"
        sources_root.mkdir()
        sources = build_installed_sources(sources_root)
        composition = modular_product_composition(sources)
        document = record_public_closure_traces(composition, root / "runs")
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
