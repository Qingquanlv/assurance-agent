"""Deterministic OpenCode model routing by workflow skill and prior failure."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.config import ModelRoutingCfg
from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.graph_types import ErrorKind
from assurance_kernel.workflow.graph.models import CompiledWorkflow

RouteSource = Literal[
    "cli_override",
    "escalation",
    "skill_route",
    "default",
    "opencode_default",
]


class ModelRouteContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    adapter: str
    skill: str
    prior_error_kind: ErrorKind | None = None
    contract_failure_kinds_seen: tuple[ErrorKind, ...] = ()
    cli_override: str | None = None


class ModelResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str | None
    source: RouteSource
    policy_sha256: str | None


class ModelRoutingError(AaError):
    pass


class ModelRouter:
    def __init__(self, policy: ModelRoutingCfg | None):
        self._policy = policy
        self._policy_sha256 = (
            None
            if policy is None
            else sha256_bytes(canonical_json_bytes(policy.model_dump(mode="json", exclude_none=True)))
        )

    def resolve(self, context: ModelRouteContext) -> ModelResolution:
        if context.cli_override is not None and context.cli_override.strip():
            return ModelResolution(
                model=context.cli_override.strip(),
                source="cli_override",
                policy_sha256=self._policy_sha256,
            )

        policy = self._policy
        if policy is not None and policy.escalation is not None:
            configured_kinds = policy.escalation.on_error_kinds
            if context.prior_error_kind in configured_kinds or any(
                kind in configured_kinds for kind in context.contract_failure_kinds_seen
            ):
                return ModelResolution(
                    model=policy.escalation.model,
                    source="escalation",
                    policy_sha256=self._policy_sha256,
                )

        if policy is not None and context.skill in policy.routes:
            return ModelResolution(
                model=policy.routes[context.skill],
                source="skill_route",
                policy_sha256=self._policy_sha256,
            )

        if policy is not None and not policy.strict_routes and policy.default is not None:
            return ModelResolution(
                model=policy.default,
                source="default",
                policy_sha256=self._policy_sha256,
            )

        if policy is None:
            return ModelResolution(
                model=None,
                source="opencode_default",
                policy_sha256=None,
            )

        raise ModelRoutingError(f"no model route for skill {context.skill!r}")

    def validate_compiled(
        self,
        compiled: CompiledWorkflow,
        *,
        adapter: str,
        cli_override: str | None,
    ) -> None:
        if adapter != "opencode":
            return
        if cli_override is not None and cli_override.strip():
            return
        policy = self._policy
        if policy is None or not policy.strict_routes:
            return

        required = _collect_skills(compiled)
        missing = sorted(skill for skill in required if skill not in policy.routes)
        if missing:
            raise ModelRoutingError("missing model routes for compiled skills: " + ", ".join(missing))


def _collect_skills(compiled: CompiledWorkflow) -> list[str]:
    skills: set[str] = set()
    for graph in compiled.graphs.values():
        for node in graph.nodes.values():
            uses = node.definition.uses
            if uses.startswith("skill:"):
                skills.add(uses.partition(":")[2])
    return sorted(skills)


__all__ = [
    "ModelResolution",
    "ModelRouteContext",
    "ModelRouter",
    "ModelRoutingError",
    "RouteSource",
]
