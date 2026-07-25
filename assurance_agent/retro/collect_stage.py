from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.phase_a import (
    IsTerminal,
    enumerate_candidates,
    snapshot_unarchived_evidence,
)
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.state import complete_retro_stage, mark_consumed_change, read_state
from assurance_agent.retro.types import LegacyRetroContext, LegacyRetroSignalSet, LegacyRetroWindow

ContextBuilder = Callable[..., LegacyRetroContext]


@dataclass(frozen=True)
class RetroCollectResult:
    retro_id: str
    retro_dir: Path
    signal_count: int
    context: LegacyRetroContext | None  # None when no candidates
    incomplete_changes: tuple[str, ...] = ()


def _make_empty_context(retro_id: str, generated_at: str) -> LegacyRetroContext:
    return LegacyRetroContext(
        retro_id=retro_id,
        generated_at=generated_at,
        window=LegacyRetroWindow(change_count=0),
        signals=LegacyRetroSignalSet(),
        signal_count=0,
    )


def run_retro_collect(
    sut: Path,
    *,
    retro_id: str,
    last: int = 10,  # reserved for graph callers to cap candidate window
    write_root: Path | None = None,
    context_builder: ContextBuilder = build_retro_context,
    is_terminal: IsTerminal | None = None,
    now: datetime | None = None,
) -> RetroCollectResult:
    """Collect retro candidates and build context; shared by nightly and graph ops.

    ``sut`` is the evidence read root; ``write_root`` (default: ``sut``) receives
    every write. The graph handler splits them: candidate evidence only exists on
    the host (coordinator files and sibling change dirs are pruned from a
    task-private tree), while writes must land in the task workspace to be frozen.

    Writes ``context.json`` unconditionally so downstream graph nodes can always
    freeze their output. Returns ``signal_count=0, context=None`` when there are
    no candidates; ``context`` is populated (and signal_count may still be 0) when
    candidates exist but carry no actionable signals.
    """
    assert_path_segment_safe(retro_id, label="retro id")
    out_root = write_root or sut
    retro_dir = out_root / "qa" / "retro" / retro_id
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if is_terminal is None:
        from assurance_agent.retro.nightly.driver import (
            _default_is_terminal,
        )  # avoid circular at module level

        is_terminal = partial(_default_is_terminal, sut)

    state = read_state(sut)
    candidates, incomplete = enumerate_candidates(sut, state, is_terminal=is_terminal)

    if not candidates:
        # Write minimal context.json so graph outputs can freeze even with no work.
        write_json(retro_dir / "context.json", _make_empty_context(retro_id, generated_at).model_dump())
        return RetroCollectResult(
            retro_id=retro_id,
            retro_dir=retro_dir,
            signal_count=0,
            context=None,
            incomplete_changes=tuple(incomplete),
        )

    for candidate in candidates:
        if candidate.evidence_source == "unarchived":
            snapshot_unarchived_evidence(sut, retro_id, candidate.change_id, dest_root=out_root)

    context = context_builder(sut, changes=[c.change_id for c in candidates], retro_id=retro_id)
    write_json(retro_dir / "context.json", context.model_dump())

    consumed_at = generated_at
    for candidate in candidates:
        mark_consumed_change(
            out_root,
            change_id=candidate.change_id,
            source=candidate.evidence_source,
            consumed_at=consumed_at,
            retro_id=retro_id,
        )

    signal_count = count_signals(context)
    if signal_count == 0:
        # Zero-signal candidates are still consumed: without finalizing the stage
        # the watermark never advances and every later retro re-reads the same
        # changes. The nightly driver used to own this; graph callers need it too.
        complete_retro_stage(out_root, retro_id)
    return RetroCollectResult(
        retro_id=retro_id,
        retro_dir=retro_dir,
        signal_count=signal_count,
        context=context,
        incomplete_changes=tuple(incomplete),
    )
