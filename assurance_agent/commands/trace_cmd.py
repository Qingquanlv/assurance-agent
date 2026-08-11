"""aa trace — fold execution-phase evidence into a `TraceProjection` (pure
read-only: on-disk fold, zero writes).

`aa status` reads the graph ledger's own projection; `aa trace` reads a
completely separate authority — the case docs, `execution/runs/**` result
files and the current `tests/` tree — via `evidence.fold_trace`. The two
paths never share a read function, matching the design doc's boundary
(`docs/superpowers/specs/2026-07-29-traceability-evidence-projection-design.md`
§4: "`evidence/` 是读侧证据投影... 与 workflow/graph 的 ledger 投影正交").

Fail-closed contract (design §10/§17) is narrower than "every row looks bad":
a missing change and "no valid case rows" both exit non-zero with a stable
message, and the latter has two distinct shapes —

- no case rows *and* no executed tests at all ("no valid case rows"), and
- no case rows but `projection.unmapped_tests` is non-empty — every test that
  ran resolved to nothing declared ("all executed tests are unmapped").

Neither shape is about row *coverage*. A projection with valid rows whose
`coverage_state` is `uncovered` or `not_required` for every single one is a
perfectly normal, displayable result and exits 0 — "this case has no live
coverage right now" is exactly the fact the projection exists to report, and
turning it into a CLI failure would make `aa trace` lie about a healthy fold
to avoid an unhealthy-sounding message. Judging whether that coverage is
*sufficient* is `aa verify`'s job (`evidence/sufficiency.py`), not this
command's.

Whichever failure applies, the projection is still rendered in full before
the command reports failure and exits non-zero: the stable error goes to
stderr, and stdout/gaps/human output are exactly what a successful run would
print for the same (empty) row set. `--only-gaps` and normal output both
honor this — there is nothing "too broken to show".

`--type`/`--only-gaps` are display-only slices: they never re-fold, never
drop a source/gap/integrity fact, and never mutate anything on disk.
`--type` only narrows the *row* list; `--only-gaps` output has no rows to
narrow, so `--type` is silently ignored when combined with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from assurance_agent.artifacts.models.trace import (
    TraceCaseType,
    TraceGapV1,
    TraceGapV2,
    TraceProjectionLike,
    TraceRow,
)
from assurance_agent.change_location import ChangeNotFoundError
from assurance_agent.config import ConfigInvalidError, ConfigNotFoundError
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_COMPLETED, EXIT_ERROR

_CASE_TYPES: tuple[TraceCaseType, ...] = ("API", "E2E", "Fuzz", "Performance")


def _filter_rows(rows: tuple[TraceRow, ...], case_type: str | None) -> tuple[TraceRow, ...]:
    if case_type is None:
        return rows
    return tuple(row for row in rows if row.case_type == case_type)


def _no_rows_error(projection: TraceProjectionLike, change_id: str) -> str | None:
    """The two "no valid case rows" fail-closed shapes (module docstring); ``None`` when fine.

    Row *coverage* (uncovered/not_required) never lands here — only the
    complete absence of rows does, and then only distinguished by whether any
    executed test exists at all (in ``unmapped_tests``) to explain why.
    """
    if projection.rows:
        return None
    if projection.unmapped_tests:
        return (
            f"trace failed: change '{change_id}' has no valid case rows: "
            "all executed tests are unmapped (present in unmapped_tests; none resolved to a "
            "declared case)"
        )
    return f"trace failed: change '{change_id}' has no valid case rows (integrity={projection.integrity})"


def _gap_line(gap: TraceGapV1 | TraceGapV2) -> str:
    parts = [gap.code, gap.source]
    if gap.batch_id:
        parts.append(f"batch={gap.batch_id}")
    if gap.target:
        parts.append(f"target={gap.target}")
    if gap.detail:
        parts.append(gap.detail)
    return "  " + " | ".join(parts)


def _print_gaps_human(gaps: tuple[TraceGapV1, ...] | tuple[TraceGapV2, ...]) -> None:
    if not gaps:
        click.echo("  (no gaps)")
        return
    for gap in gaps:
        click.echo(_gap_line(gap))


def _print_rows_human(rows: tuple[TraceRow, ...]) -> None:
    if not rows:
        click.echo("  (no rows)")
        return
    for row in rows:
        latest = row.latest_execution
        status = latest.status if latest is not None else "(never executed)"
        click.echo(
            f"  {row.case_id:<20} type={row.case_type:<11} coverage={row.coverage_state:<12} latest={status}"
        )


def _render_only_gaps(projection: TraceProjectionLike, change_id: str, *, as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                [gap.model_dump(mode="json") for gap in projection.gaps],
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    click.secho(f"aa trace --only-gaps — change: {change_id}", bold=True)
    click.echo()
    _print_gaps_human(projection.gaps)
    click.echo()


def _render_json(projection: TraceProjectionLike, rows: tuple[TraceRow, ...]) -> None:
    payload = projection.model_dump(mode="json")
    payload["rows"] = [row.model_dump(mode="json") for row in rows]
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _render_human(projection: TraceProjectionLike, rows: tuple[TraceRow, ...], change_id: str) -> None:
    click.secho(f"aa trace — change: {change_id}", bold=True)
    click.echo()
    click.echo(f"  integrity          : {projection.integrity}")
    click.echo(f"  authoritative_batch: {projection.authoritative_batch_id or '(none)'}")
    click.echo()
    _print_rows_human(rows)
    if projection.gaps:
        click.echo()
        click.echo(f"  gaps ({len(projection.gaps)}):")
        _print_gaps_human(projection.gaps)
    click.echo()


def _run_trace(change_id: str, as_json: bool, case_type: str | None, only_gaps: bool) -> int:
    project_root = Path.cwd()
    try:
        projection = fold_trace(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError, ConfigInvalidError) as err:
        click.secho(str(err), fg="red", err=True)
        return EXIT_ERROR

    error = _no_rows_error(projection, change_id)
    rows = _filter_rows(projection.rows, case_type)

    # Render the requested view first — a stable error still describes an
    # otherwise fully-rendered (if row-empty) projection, not a swallowed one.
    if only_gaps:
        _render_only_gaps(projection, change_id, as_json=as_json)
    elif as_json:
        _render_json(projection, rows)
    else:
        _render_human(projection, rows, change_id)

    if error is not None:
        click.secho(error, fg="red", err=True)
        return EXIT_ERROR
    return EXIT_COMPLETED


@click.command("trace")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.option(
    "--type",
    "case_type",
    type=click.Choice(_CASE_TYPES),
    default=None,
    help=(
        "Show only rows of one case type (API/E2E/Fuzz/Performance); gaps/integrity stay "
        "unfiltered, and this option is ignored entirely when combined with --only-gaps "
        "(there are no rows to filter in gap-only output)."
    ),
)
@click.option(
    "--only-gaps",
    "only_gaps",
    is_flag=True,
    help="Print only the TraceGap list — what evidence is missing, corrupt or contradicted.",
)
def trace_command(change_id: str, as_json: bool, case_type: str | None, only_gaps: bool) -> None:
    """Fold execution-phase evidence into a TraceProjection (pure read-only, no writes)."""
    raise SystemExit(_run_trace(change_id, as_json, case_type, only_gaps))
