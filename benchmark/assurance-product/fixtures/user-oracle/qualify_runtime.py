"""Emit a closed qualification record for the User oracle runtime."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from importlib.metadata import distributions
from pathlib import Path

_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)\s*\\?$")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_distributions(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _REQUIREMENT.fullmatch(line)
        if match is None:
            continue
        name, version = match.groups()
        locked[_normalized(name)] = version
    if not locked:
        raise RuntimeError("runtime dependency lock has no active distributions")
    return dict(sorted(locked.items()))


def qualify(sut_dir: Path, sqlite_path: Path) -> dict[str, object]:
    sut = Path(sut_dir).resolve(strict=True)
    sqlite = Path(sqlite_path).resolve(strict=True)
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("User oracle requires Python 3.11")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("User oracle requires an isolated Python environment")
    locked = _locked_distributions(sut / "requirements.lock")
    installed = {
        _normalized(str(item.metadata["Name"])): item.version
        for item in distributions()
        if item.metadata["Name"]
    }
    if installed != locked:
        raise RuntimeError("installed distributions do not exactly match requirements.lock")
    sys.path.insert(0, str(sut))
    import app
    from app.settings import settings

    sqlite_config = settings.TORTOISE_ORM["connections"]["sqlite"]
    configured = Path(sqlite_config["credentials"]["file_path"]).resolve(strict=False)
    if sqlite_config["engine"] != "tortoise.backends.sqlite" or configured != sqlite:
        raise RuntimeError("effective Tortoise SQLite configuration does not match managed DB")
    executable = Path(sys.executable).absolute()
    details = executable.stat()
    prefix = Path(sys.prefix).resolve(strict=True)
    prefix_details = prefix.stat()
    return {
        "schema_version": "1",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_executable": str(executable),
        "python_device": details.st_dev,
        "python_inode": details.st_ino,
        "python_size": details.st_size,
        "python_mtime_ns": details.st_mtime_ns,
        "python_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_prefix": str(prefix),
        "python_prefix_device": prefix_details.st_dev,
        "python_prefix_inode": prefix_details.st_ino,
        "distributions": locked,
        "app_module": str(Path(app.__file__).resolve(strict=True)),
        "sqlite_engine": sqlite_config["engine"],
        "sqlite_path": str(configured),
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: qualify_runtime.py SUT_DIR SQLITE_PATH")
    print(json.dumps(qualify(Path(sys.argv[1]), Path(sys.argv[2])), sort_keys=True))
