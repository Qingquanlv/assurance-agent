"""python -m assurance_kernel --product sample compile"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="assurance_kernel")
    parser.add_argument("--product", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("compile")
    args = parser.parse_args(argv)

    from assurance_kernel.product import ProductError, select_product
    from assurance_kernel.workflow.graph.compiler import compile_loaded_workflow
    from assurance_kernel.workflow.graph.schema_v2 import load_workflow_v2_with_origin

    if args.product is None:
        try:
            select_product("assurance")
        except ProductError as err:
            raise SystemExit(f"{err}; pass --product <id>") from err
    else:
        select_product(args.product)

    if args.command == "compile":
        loaded = load_workflow_v2_with_origin(Path.cwd())
        compile_loaded_workflow(loaded, contracts=None)
        print("ok")


if __name__ == "__main__":
    main()
