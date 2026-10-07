# Attempt Runtime / domain handler separation

## Authority and scope

The user explicitly requested completion of the missing Pi Durable-inspired Attempt refactor. The original Spike required separating state progression from Assurance operations. Later decisions remove Effects, prohibit concurrent workers, and regenerate abandoned production nodes; those decisions do not cancel internal separation. This supplement reconciles both requirements.

This is an internal Python refactor, not adoption of the TypeScript Pi package. Reference: https://github.com/earendil-works/pi/blob/56b25ff4ebbd8a119ef9185447c7b2416059dbcb/packages/durable/src/harness/tool.ts (handler-controlled commits and distinct first-execution/recovery entries).

## Required result

1. `AssuranceAttemptKernel.execute_or_recover` remains the graph-facing facade with its existing signature and constructor compatibility. It composes a separate Attempt runtime and domain handlers. It no longer owns the transaction orchestration or side-effect protocols.
2. The runtime owns deterministic action selection and continuation/return decisions. Decisions use the existing journal-derived phase/state, with explicitly local invocation prerequisites if required. Expose a pure, testable decision function. Never use phase equality as proof of progress. Keep execution bounded and sequential; no polling loop or second graph scheduler.
3. Domain handlers own authorization, workspace/activity operations, validation/prepare/promotion, terminal publication and release. Extract by responsibility, not one forwarding class per function. Keep each atomic append, fence check, cut point and durability barrier where the relevant transaction protocol requires it. The runtime must not perform a blanket `persist(result)` or translate arbitrary exceptions to failures.
4. New invocation-local state must be typed and scoped to a single call, never retained on the reusable kernel. A pending/indeterminate result returns immediately. A handler may commit multiple journal records without changing coarse phase. Prepared/promotion recovery must rebuild necessary authorization/workspace inputs, not skip them based solely on phase.
5. Journal remains the only durable truth; no persisted phase/checkpoint or journal schema change. Preserve literal golden journal bytes, event order, existing transaction trace, crash-cut names, exception identity and release evidence. Existing low-level same-Attempt recovery remains supported; production generation allocation and retry budgets remain at AttemptNodeFactory; repair remains in Flow; live owner/stop remain in product.
6. Do not restore Effect/Intent, concurrent worker takeover, a new generic ReplayPolicy framework, package dependencies, plugin handler registry, configurable scheduling, or graph DSL. Dispatch vs reconcile/adopt-result choices are explicit in the decision model; domain-specific proof stays with handlers.

## Acceptance

- Pure routing tests cover fresh/authorized, prepared/dispatch_started/bound, observed, prepared commit, promoted, terminal/released, and required call-local setup.
- Driver tests with small recording handlers prove finite progression, immediate pending/indeterminate return, no activity replay on terminal, and unmodified exception propagation after promotion. They must exercise runtime behavior, not merely assert module layout.
- Existing golden-byte, crash/recovery, drift, validator, evidence, stop, retry/regeneration tests remain authoritative and unchanged in meaning.
- Architectural boundary is enforced: runtime cannot depend on concrete domain handlers, workspace, validators or resource arbiter operations; facade composes both.
- Full repository checks and all three wheel smokes run on final source. Final report maps each original target to implementation and identifies retained/deferred policy ownership explicitly.

## Selected code shape

The runtime uses an explicit action enum and a pure `select_action(snapshot, progress)` function. `progress` records actions completed within this call only. Every call reconstructs its prerequisites, even when the persisted snapshot is already prepared or promoted. The runtime invokes one handler action at a time and accepts either an updated snapshot or a returned resolution. A finite action bound prevents an accidental internal cycle; no phase/revision equality test controls repetition.

The facade creates the runtime and domain handlers per call from its current public ports. Authorization/activity, commit and terminal/release form cohesive transaction boundaries. The commit handler retains output observation, validator execution, durable prepare and promotion checks. Typed per-call state carries workspace and transaction results; reusable kernel instances do not retain it.

## Original Spike requirement mapping

| Original goal | Current decision |
| --- | --- |
| Explicit durable phase | Existing journal-derived `AttemptPhase`; no second checkpoint |
| Pure state/action selection | Runtime selects actions from snapshot and local prerequisites |
| Runtime / domain split | Runtime controls progression; handlers control operations and commits |
| Handler controls commit timing | Existing append groups, barriers and cuts stay inside each transaction protocol |
| Retry vs repair | Technical retry remains in AttemptNodeFactory with durable generation budget; business repair remains in Flow |
| Resume vs reconcile | Resume continues a paused graph; low-level reconcile observes/adopts an already-started activity without another dispatch |
| Production interruption | Confirm old execution stopped, clean its resources, allocate a fresh Attempt and regenerate |
| Replay policy | Explicit dispatch/reconcile/adopt-result decisions; no new public policy hierarchy |
| Effect handling | Removed by later decision; not reintroduced |
| Fencing and write integrity | Preserve current CAS, authorization checks and file proofs |

The later regeneration decision changes the production restart policy. It does not remove the existing lower-level same-Attempt recovery API or move graph routing into the Attempt runtime.
