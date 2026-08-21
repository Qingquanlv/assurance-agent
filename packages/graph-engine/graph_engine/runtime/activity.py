from graph_engine.errors import GraphEngineError


class TaskActivityConflict(GraphEngineError):
    """Raised for an illegal or competing activity transition."""


class TaskActivityIndeterminate(GraphEngineError):
    """Raised when an external activity outcome cannot be proven."""


class TaskActivityReferenceInvalid(GraphEngineError):
    """Raised when a reference is non-canonical, oversized, or changed."""


class TaskActivityRecoveryUnsupported(GraphEngineError):
    """Raised when a running activity lacks the required recoverable protocol."""


class AttemptWorkspaceLost(GraphEngineError):
    """Raised when the prepared attempt workspace cannot be authenticated."""


class TaskActivityProtocolViolation(GraphEngineError):
    """Raised when a handler result violates the closed activity contract."""


__all__ = [
    "AttemptWorkspaceLost",
    "TaskActivityConflict",
    "TaskActivityIndeterminate",
    "TaskActivityProtocolViolation",
    "TaskActivityRecoveryUnsupported",
    "TaskActivityReferenceInvalid",
]
