"""Parse engine-injected candidate bytes for semantic validators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from graph_engine.plugin_api import ValidationResult


def load_json(file_bytes: Mapping[str, bytes], path: str) -> object | None:
    raw = file_bytes.get(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def bytes_match_digest(raw: bytes, digest: str | None) -> bool:
    if digest is None:
        return False
    hex_digest = hashlib.sha256(raw).hexdigest()
    return digest in {hex_digest, f"sha256:{hex_digest}"}


def rejected(reason: str) -> ValidationResult:
    return ValidationResult(accepted=False, reason=reason)
