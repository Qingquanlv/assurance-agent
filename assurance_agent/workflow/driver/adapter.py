"""Phase-agent adapter contract (spec 5a).

A phase adapter abstracts "hand one workflow phase to some Agent and get a
result back". Two implementations follow: HeadlessAdapter (subprocess) and
OpenCodeAdapter (HTTP). The deterministic loop only ever sees this Protocol.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from assurance_agent.exceptions import AaError


class DriverError(AaError):
    """Any driver-layer failure surfaced to the CLI as EXIT_ERROR."""


class PhaseRequest(BaseModel):
    change_id: str
    phase_id: str
    skill: str | None = None
    agent: str | None = None
    prompt: str


class PhaseResult(BaseModel):
    ok: bool
    output: str = ""
    error: str | None = None


@runtime_checkable
class Adapter(Protocol):
    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        """Dispatch one phase to an Agent; never raises for an agent-side
        failure — return PhaseResult(ok=False, error=...) instead."""
        ...
