"""Resource refs capability wheels share. The product model stays in the product."""

from __future__ import annotations

from pydantic import Field

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import SHA256_PATTERN


class PolicyResourceV1(FrozenModel):
    """Product policy identity. Attempt JSON is ``resource_id`` and ``sha256``."""

    resource_id: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


__all__ = ["PolicyResourceV1"]
