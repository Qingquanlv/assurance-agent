"""Atomic temp-then-replace writes for kernel and domain publishers."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write via temp-then-replace so no reader ever sees a half-written file.

    The single writer for everything one execution batch publishes, and for the
    change-level trace documents the ``materialize-trace-projection`` node publishes
    after issue reconciliation.

    Atomic *per file*, which is the only guarantee ``os.replace`` can give: a caller
    publishing two related documents makes two calls and can be interrupted between
    them. Ordering is what such callers rely on — the trace node writes the
    projection before the facts that summarise it, so a reader who finds the facts
    always finds the projection they came from.

    The temp name carries the writing process and thread because a name derived
    from the target alone is shared by concurrent writers, and two writers
    sharing one temp file do not merely lose a temp file: the first ``replace``
    consumes it and the second fails after the first has already published.
    Cleanup never raises, so a failed publication is what the caller sees rather
    than the failure to tidy up after it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
