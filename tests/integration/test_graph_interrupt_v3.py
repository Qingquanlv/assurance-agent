"""Graph interrupt/resume 集成测试：event_schema_version=3 + ResumeAnchor wire。"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.models import ResumeCommand
from tests.helpers_graph_v3 import (
    assert_event_schema_version_3,
    assert_v3_interrupt_anchors,
    assert_v3_resume_anchor_chain,
)
from tests.unit.workflow.graph.test_subgraph_interrupt import (
    _THREE_LEVEL_INTERRUPT,
    _build_runtime,
    _compile,
    _context,
    _make_project,
    _ops,
)

_SINGLE_INTERRUPT = """
main:
  max_supersteps: 8
  nodes:
    seed:
      uses: operation:write-review
      outputs: [change:review/case-review.json]
      retry: never
      timeout: local
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: seed}
    - {from: seed, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_new_run_pins_event_schema_version_3(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_SINGLE_INTERRUPT)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    events = read_events_strict(_context(project).change_dir)
    assert_event_schema_version_3(events)
    assert_v3_interrupt_anchors(events)


def test_single_level_resume_emits_v3_anchor(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_SINGLE_INTERRUPT)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    context = _context(project)
    result = runtime.run(compiled, "full", context)
    interrupt = result.status.pending_interrupts[0]

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0
    events = read_events_strict(context.change_dir)
    assert_v3_resume_anchor_chain(events, interrupt.checkpoint_ns)


def test_resume_payload_passes_through_to_first_layer(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_SINGLE_INTERRUPT)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    context = _context(project)
    result = runtime.run(compiled, "full", context)
    interrupt = result.status.pending_interrupts[0]

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept",
            who="reviewer",
            payload={"waiver_id": "W-42", "notes": "risk accepted"},
        ),
    )
    assert done.exit_code == 0
    events = read_events_strict(context.change_dir)
    resumed = [e for e in events if e.get("type") == "graph_resumed"]
    assert resumed, "expected graph_resumed events"

    def _seq(event: dict[str, object]) -> int:
        seq = event.get("seq")
        return seq if isinstance(seq, int) and not isinstance(seq, bool) else 0

    first = min(resumed, key=_seq)
    assert first.get("payload") == {"waiver_id": "W-42", "notes": "risk accepted"}
    # Non-root layers (if any) must not copy the payload.
    for event in resumed:
        if event.get("parent_anchor_ref") is not None:
            assert event.get("payload") in (None, {})


def test_three_level_nested_interrupt_resume_v3_integration(tmp_path: Path) -> None:
    """Fresh-process style: run → interrupt → resume completes with per-layer v3 anchors."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_THREE_LEVEL_INTERRUPT)
    context = _context(project)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())

    result = runtime.run(compiled, "full", context)
    assert result.exit_code == 30
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.checkpoint_ns.count("/") >= 4

    events = read_events_strict(context.change_dir)
    assert_event_schema_version_3(events)
    assert_v3_interrupt_anchors(events)

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept through nested layers",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0, done.reason
    assert done.status.status == "completed"
    assert done.status.pending_interrupts == ()

    events = read_events_strict(context.change_dir)
    assert_v3_resume_anchor_chain(events, interrupt.checkpoint_ns)


def test_fresh_runtime_replays_v3_resume_projection(tmp_path: Path) -> None:
    """Second GraphRuntime instance must fold v3 resume events and reach completed."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_THREE_LEVEL_INTERRUPT)
    context = _context(project)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", context)
    interrupt = result.status.pending_interrupts[0]
    runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept",
            who="reviewer",
        ),
    )

    fresh = _build_runtime(project, compiled, contracts, ops=_ops())
    status = fresh.status(result.invocation_id)
    assert status.status == "completed"
    assert status.pending_interrupts == ()
