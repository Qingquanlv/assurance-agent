# ADR 0001: Driver commits phase outcomes in-process

## Status

Accepted — 2026-07-15

## Context

The TypeScript assurance-workflow driver advanced phase state by shelling
`aa state apply` after each successful skill/cli phase. That kept the CLI as the
sole write authority and matched an earlier “M6 driver is the only transaction
boundary” framing.

The Python migration concentrated the real commit protocol
(snapshot → files → strict events → state → restore on failure) into
`workflow.core.progression.transaction`, with domain operations in
`workflow.orchestration.operations`. Keeping a subprocess hop for driver
outcomes would:

- duplicate two write paths in one process (healing allocation already wrote
  in-process; outcomes would still shell);
- pay fork/exec cost on every phase with no additional audit safety;
- force tests to fake a CLI binary for what is now a pure library call.

Architecture review and grilling (Q6) chose in-process `apply_phase_outcome`
for the driver. CLI `aa state apply` remains for humans/agents; it calls the
same operation.

## Decision

`DefaultCliPhaseExecutor.apply_phase_state` calls `apply_phase_outcome` in
process. Subprocess `aa` is reserved for cli-kind phases (`aa run`,
`aa report …`). Audit atomicity for catchable failures is owned by
`progression.transaction`, not by “CLI as write authority.”

## Consequences

- Driver and CLI share one operations surface; `InProcessAa` test doubles are
  unnecessary.
- Future reviews must not reintroduce a subprocess hop solely for historical
  TS parity; reopen only if a hard process isolation requirement appears.
- Performance is a side benefit, not the primary motivation — correctness and
  a single write boundary are.
