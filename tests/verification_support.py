from __future__ import annotations

import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "verification"


def read_fixture(name: str) -> dict[str, object]:
    if name not in {"user-case.json", "user-sources.json", "user-plan.json"}:
        raise ValueError("unknown verification fixture")
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


__all__ = ["read_fixture"]
