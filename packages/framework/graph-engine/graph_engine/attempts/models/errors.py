from graph_engine.errors import GraphEngineError


class AttemptIdentityDrift(GraphEngineError):
    """Raised when replay identity does not match the opened Attempt."""


class AttemptIntegrityError(GraphEngineError):
    """Raised when durable terminal/release proof contradicts the authorization store."""
