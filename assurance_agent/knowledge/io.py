"""Atomic L1 load/write with comment-preserving round-trip (spec C5)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def _yaml() -> YAML:
    yml = YAML()
    yml.preserve_quotes = True
    yml.default_flow_style = False
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def load_l1_document(path: Path) -> Any:
    yml = _yaml()
    text = path.read_text(encoding="utf-8")
    return yml.load(text) or {}


def write_l1_document(path: Path, document: Any) -> None:
    yml = _yaml()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        yml.dump(document, handle)
    os.replace(tmp, path)
