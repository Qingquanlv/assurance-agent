# Attempt Kernel Internal Refactor Design Note

> **Status:** deferred responsibility-boundary decision; no implementation shape is frozen.
>
> **Date:** 2026-09-03. The requested historical filename is retained.
>
> **Starts after:** [Attempt Runtime Production Closure](./2026-09-03-attempt-runtime-production-closure-design.md)
> is implemented and the ordinary repository gate is green on a clean base.
>
> **Amended 2026-09-04:** candidate-bound live certification was removed by
> [Checkpoint R Removal Design](./2026-09-04-checkpoint-r-removal-design.md). It is not a Phase I
> dependency and must not be recreated under another name.

## Decision

`AssuranceAttemptKernel.execute_or_recover(...)` remains the one graph-facing transaction API.
After production closure, its implementation may be reorganized to make its existing
responsibilities easier to understand and test. This note freezes responsibility boundaries and
semantic constraints only. It does not require a particular class count, module count, naming
scheme, or line-count reduction.

The concrete decomposition must be chosen from the Phase P code that actually exists. A future
implementation plan may keep a responsibility as private functions, extract a value object, or
create a private collaborator. No extraction is justified solely to make a diagram symmetrical.

## Stable public boundary

All graph execution callers continue to use only:

```python
AssuranceAttemptKernel.execute_or_recover(
    attempt_key,
    resolved_contract,
    validated_input,
    execution_context,
) -> AttemptResolution
```

The refactor must not create another Workflow scheduler, LangGraph node, transaction coordinator,
or public service API. LangGraph retains Workflow control; the Kernel retains one Attempt's
transaction order and recovery decision.

Dependency assembly may bind the same authoritative `AttemptJournalPort` to the Kernel and
`AttemptNodeFactory`, but graph code may not call another concrete Kernel method.

## Responsibility boundaries

The implementation must keep the following responsibilities identifiable and prevent authority
from leaking between them:

| Responsibility | Owns | Must not own |
| --- | --- | --- |
| journal and replay | Attempt identity, snapshot folding, CAS append, durable replay and terminal proof | workspace promotion, external activity, Effect business logic |
| authorization and fencing | grant acquisition/adoption, live-fence assertions, terminal release | output validation, graph routing, activity protocol |
| activity | external execute/adopt/reconcile and observation of the source host's terminal receipt | seal, promotion, terminal publication |
| commit | final output/intent validation, seal, ordered Validators, durable prepare, promote/recover, and the canonical `ActivityTerminalObserved` + `EffectIntentRecorded` durability fallback | OpenCode admission, external Effect application |
| Effect settlement | ordered apply/reconcile of persisted intents and Effect receipts | discovery of process-local intents, graph routing, workspace mutation |

This table is normative; boxes drawn around the rows are not. Two rows may remain in one private
module when their implementation is cohesive. One row may require more than one private type when
its state machine demands it.

## Invariants

The refactor preserves, byte-for-byte where serialized, all Phase P behavior:

1. one business activation derives one stable `AttemptKey`;
2. one Attempt admits at most one external Agent prompt;
3. stale fences cannot dispatch, publish, promote, settle Effects, or release newer grants;
4. observed activity success is not committed Attempt success;
5. output and Effect intents are durable before sealing and independent of executor memory;
6. Validators run before durable prepare and promotion;
7. Effects run only after promotion and reconcile by the same settlement identity;
8. terminal replay is returned only after durable resource-release proof;
9. pending and indeterminate results keep their existing LangGraph interrupt mapping;
10. every crash cut resumes the same Attempt without duplicate prompt, promotion, Effect, or
    terminal result.

The event schema, persisted record schema, resolution types, transaction trace, ProductLock, and
GraphRevision are not redesigned in this phase.

## Extraction rules

- Start from characterization tests over the production implementation, including every crash cut.
- Extract around state ownership or an irreversible boundary, not around each Kernel method.
- Keep storage behind the existing durable ports; do not wrap a port only to rename its methods.
- Pass the narrowest state needed by each responsibility. Do not introduce a generic service
  locator or dependency bag.
- Keep ephemeral authorization/activity context out of serialized domain records unless Phase P
  already serializes it.
- Do not duplicate the `AttemptEffectSettler`, Effect registry, workspace state machine, or
  resource store for interface symmetry.
- A private helper may be deleted or folded back when extraction makes the call graph longer
  without hiding meaningful complexity.

## Implementation decision point

Only after Phase P is complete, the implementation plan must inspect the current Kernel and answer:

1. Which responsibility clusters already have cohesive private APIs?
2. Which state crosses an irreversible boundary and therefore deserves an explicit value?
3. Which duplicated condition or recovery branch becomes single-sourced after extraction?
4. Does each proposed collaborator hide complexity from the coordinator, or merely forward calls?
5. Can the same clarity be achieved with private functions and a smaller diff?

The resulting plan records the chosen code shape and the rejected alternatives. This note does not
pre-approve four classes, the names from an earlier diagram, or a proposed file tree.

## Acceptance

- all graph execution callers still depend only on `execute_or_recover`;
- the normative transaction trace and all crash/recovery outcomes are unchanged;
- no serialized schema or public protocol changes merely to enable extraction;
- responsibility ownership is evident in code and covered by focused tests;
- no new LangGraph node, scheduler, factory registry, or service-locator abstraction appears;
- focused characterization, journal-byte, crash/replay, the full repository gate, and all wheel
  smoke tests pass for the clean refactor candidate;
- review judges the resulting code shape, not compliance with an arbitrary class or line count.

Phase I is complete only when the refactor is behavior-preserving. Production closure does not wait
for it, and completing this note alone makes no production-readiness claim.
