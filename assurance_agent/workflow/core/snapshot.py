"""Small file snapshots used by the M6 progression write boundary."""
from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    existed: bool
    content: bytes | None


def capture_files(paths: Sequence[Path]) -> tuple[FileSnapshot, ...]:
    return tuple(
        FileSnapshot(
            path=path,
            existed=path.exists(),
            content=path.read_bytes() if path.is_file() else None,
        )
        for path in paths
    )


def restore_files(snapshots: Sequence[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.existed:
            if snapshot.path.is_dir():
                shutil.rmtree(snapshot.path, ignore_errors=True)
            else:
                snapshot.path.unlink(missing_ok=True)
            continue
        if snapshot.content is None:
            continue
        if snapshot.path.is_dir():
            shutil.rmtree(snapshot.path)
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot.path.with_suffix(snapshot.path.suffix + f".restore.{os.getpid()}")
        tmp.write_bytes(snapshot.content)
        os.replace(tmp, snapshot.path)
