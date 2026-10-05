from graph_engine.errors import GraphEngineError


class AttemptIdentityDrift(GraphEngineError):
    """Raised when replay identity does not match the opened Attempt."""


__all__ = ["AttemptIdentityDrift"]
