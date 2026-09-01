from __future__ import annotations

from pydantic import Field

from graph_engine.attempts.keys import AttemptKey
from graph_engine.plugin_api import FrozenModel


class AttemptExecutionContext(FrozenModel):
    invocation_id: str = Field(min_length=1)
    public_entrypoint: str = Field(min_length=1)
    semantic_node_id: str = Field(min_length=1)
    attempt_key: AttemptKey
    fencing_token: int = Field(ge=1)
    authorization_id: str | None = Field(default=None, min_length=1)


__all__ = ["AttemptExecutionContext"]
