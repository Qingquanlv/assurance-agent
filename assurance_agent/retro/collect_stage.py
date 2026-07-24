from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.phase_a import IsTerminal, enumerate_candidates, snapshot_unarchived_evidence
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.state import mark_consumed_change, read_state
from assurance_agent.retro.types import RetroContext, RetroSignalSet, RetroWindow

ContextBuilder = Callable[..., RetroContext]


@dataclass(frozen=True)
class RetroCollectResult:
    retro_id: str
    retro_dir: Path
    signal_count: int
    context: RetroContext | None  # None when no candidates


def _make_empty_context(retro_id: str, generated_at: str) -> RetroContext:
    return RetroContext(
        retro_id=retro_id,
        generated_at=generated_at,
        window=RetroWindow(change_count=0),
        signals=RetroSignalSet(),
        signal_count=0,
    )


def run_retro_collect(
    sut: Path,
    *,
    retro_id: str,
    last: int = 10,  # reserved for graph callers to cap candidate window
    context_builder: ContextBuilder = build_retro_context,
    is_terminal: IsTerminal | None = None,
    now: datetime | None = None,
) -> RetroCollectResult:
    """Collect retro candidates and build context; shared by nightly and graph ops.

    Writes ``context.json`` unconditionally so downstream graph nodes can always
    freeze their output. Returns ``signal_count=0, context=None`` when there are
    no candidates; ``context`` is populated (and signal_count may still be 0) when
    candidates exist but carry no actionable signals.
    """
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut / "qa" / "retro" / retro_id
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if is_terminal is None:
        from assurance_agent.retro.nightly.driver import _default_is_terminal  # avoid circular at module level

        is_terminal = partial(_default_is_terminal, sut)

    state = read_state(sut)
    candidates, _incomplete = enumerate_candidates(sut, state, is_terminal=is_terminal)

    if not candidates:
        # Write minimal context.json so graph outputs can freeze even with no work.
        write_json(retro_dir / "context.json", _make_empty_context(retro_id, generated_at).model_dump())
        return RetroCollectResult(retro_id=retro_id, retro_dir=retro_dir, signal_count=0, context=None)

    for candidate in candidates:
        if candidate.evidence_source == "unarchived":
            snapshot_unarchived_evidence(sut, retro_id, candidate.change_id)

    context = context_builder(sut, changes=[c.change_id for c in candidates], retro_id=retro_id)
    write_json(retro_dir / "context.json", context.model_dump())

    consumed_at = generated_at
    for candidate in candidates:
        mark_consumed_change(
            sut,
            change_id=candidate.change_id,
            source=candidate.evidence_source,
            consumed_at=consumed_at,
            retro_id=retro_id,
        )

    signal_count = count_signals(context)
    return RetroCollectResult(
        retro_id=retro_id, retro_dir=retro_dir, signal_count=signal_count, context=context
    )
