"""Parse host-only documents without retaining secret-bearing exceptions."""

from __future__ import annotations

import hashlib
from typing import TypeVar

from pydantic import BaseModel
from graph_engine.plugin_api import SecretPort

Model = TypeVar("Model", bound=BaseModel)


class HostSecretDocumentError(ValueError):
    """A fixed error category suitable for reporting outside the host boundary."""


def read_host_secret_model(
    secret_port: SecretPort | None, handle: str, model: type[Model], *, category: str
) -> tuple[Model, str]:
    raw = None
    try:
        if secret_port is not None:
            raw = secret_port.resolve(handle)
    except Exception:
        pass
    if raw is None:
        raise HostSecretDocumentError(f"{category} is unavailable")
    parsed = None
    try:
        parsed = model.model_validate_json(raw)
    except (ValueError, TypeError):
        pass
    if parsed is None:
        raw = None
        # Raise outside the handler: __context__ must not retain ValidationError.
        raise HostSecretDocumentError(f"{category} is invalid")
    return parsed, hashlib.sha256(raw).hexdigest()
