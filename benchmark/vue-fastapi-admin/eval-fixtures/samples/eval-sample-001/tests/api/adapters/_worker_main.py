"""Subprocess entrypoint for isolated_worker Tortoise operations against live SUT SQLite."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tortoise import Tortoise

from app.settings import TORTOISE_ORM


def _resolve_sqlite_path(cli_override: str | None) -> str | None:
    if cli_override:
        return cli_override
    env_path = os.getenv("QA_SQLITE_FILE")
    return env_path if env_path else None


def _tortoise_config(sqlite_file: str | None) -> dict:
    if not sqlite_file:
        return TORTOISE_ORM
    config = copy.deepcopy(TORTOISE_ORM)
    config["connections"]["sqlite"]["credentials"]["file_path"] = sqlite_file
    return config


async def _invoke(func_path: str, kwargs: dict, sqlite_file: str | None) -> object:
    await Tortoise.init(config=_tortoise_config(sqlite_file))
    try:
        module_path, func_name = func_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        return await func(**kwargs)
    finally:
        try:
            conn = Tortoise.get_connection("sqlite")
            await conn.execute_query("PRAGMA wal_checkpoint(FULL)")
        finally:
            await Tortoise.close_connections()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--func", required=True)
    parser.add_argument("--args", default="{}")
    parser.add_argument("--sqlite-file", default=None)
    ns = parser.parse_args()
    kwargs = json.loads(ns.args)
    sqlite_file = _resolve_sqlite_path(ns.sqlite_file)
    try:
        result = asyncio.run(_invoke(ns.func, kwargs, sqlite_file))
        print(json.dumps(result))
    except Exception as exc:  # noqa: BLE001 — worker must surface domain failures to parent
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
