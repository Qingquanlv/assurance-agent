"""Detached worker for one operator run. Invoked as ``python -m assurance_product.operator_worker``."""

from __future__ import annotations

import sys
from pathlib import Path

from assurance_product.operator import serve_run


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: python -m assurance_product.operator_worker RUN_DIR")
    from assurance_product.worker_lifecycle import admit_background, run_workspace

    run_dir = Path(args[0])
    workspace, invocation = run_workspace(run_dir)
    with admit_background(workspace, invocation, run_dir=run_dir):
        serve_run(run_dir)


if __name__ == "__main__":
    main()
