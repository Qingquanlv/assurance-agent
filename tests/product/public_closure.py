from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_engine.attempts.activity import InvocationProjection

PUBLIC_CLOSURE_GOLDEN = Path(__file__).resolve().parent / "goldens" / "public-closure.json"

PUBLIC_ENTRYPOINTS = (
    "intake",
    "case",
    "full",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)

STANDALONE_PASS_SCENARIOS: tuple[dict[str, Any], ...] = tuple(
    {"entrypoint": name} for name in PUBLIC_ENTRYPOINTS if name not in {"full", "execute"}
)

PUBLIC_CLOSURE_SCENARIOS: tuple[dict[str, Any], ...] = (
    *STANDALONE_PASS_SCENARIOS,
    {"entrypoint": "intake", "review_decision": "needs_human_review"},
)


def requires_terminal_output(scenario: dict[str, Any]) -> bool:
    return str(scenario["entrypoint"]) in {"full", "execute"}


def jsonable_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"entrypoint": scenario["entrypoint"]}
    for key in ("review_decision", "healing_decision", "threshold", "coverage_rounds"):
        if key in scenario:
            payload[key] = scenario[key]
    for key in ("execution_sequence", "coverage_sequence"):
        if key in scenario:
            payload[key] = list(scenario[key])
    return payload


def _interrupts(projection: InvocationProjection) -> tuple[tuple[str, tuple[str, ...]], ...]:
    seen: list[tuple[str, tuple[str, ...]]] = []
    for activation in projection.activations:
        if activation.interrupt_reason is None:
            continue
        seen.append((activation.interrupt_reason, tuple(activation.interrupt_actions)))
    if projection.pending_interrupt is not None:
        pending = (
            projection.pending_interrupt.reason,
            tuple(projection.pending_interrupt.actions),
        )
        if pending not in seen:
            seen.append(pending)
    return tuple(seen)


def _effects(projection: InvocationProjection) -> tuple[tuple[str, str], ...]:
    return tuple((item.kind, item.status) for item in projection.effects)
