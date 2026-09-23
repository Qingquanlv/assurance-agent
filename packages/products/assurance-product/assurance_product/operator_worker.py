"""Detached worker for one operator run. Invoked as ``python -m assurance_product.operator_worker``."""

from __future__ import annotations

import sys
from pathlib import Path

from assurance_product.operator import serve_run


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: python -m assurance_product.operator_worker RUN_DIR")
    serve_run(Path(args[0]))


if __name__ == "__main__":
    main()
