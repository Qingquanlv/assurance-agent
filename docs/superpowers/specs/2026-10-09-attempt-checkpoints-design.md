# Attempt checkpoints and phase handlers

## Approved scope

The user approved the six-step migration in the conversation and explicitly asked to implement it in a new worktree from remote main. This document records that approved design. It supersedes the previous runtime-separation design's journal-only constraint.

Attempt execution and recovery use a complete persisted checkpoint. Its `phase` directly identifies the next/recovery handler. The old Attempt event journal, event folding, derived phase and action-selection machinery are removed after all readers move. This borrows the checkpoint/phase-handler design from Pi Durable; no Pi dependency is added.

Reference: `earendil-works/pi` revision `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`, `packages/durable/src/types.ts`, `harness/scheduler.ts`, and `harness/tool.ts`. A handler can commit its recovery phase before an external call and continue executing that call. Saving the phase does not itself invoke another handler.

## Invariants

- Python 3.11 and the existing uv workspace. No new dependency.
- Python wheels own graph topology, handlers, contracts and policy. Do not load implementations from the SUT.
- Flow topology, technical Attempt identity, retry budgets, business loop budgets and public resolution meanings remain unchanged.
- Keep the existing single-worker admission, live authority checks, workspace validation and commit/release proofs.
- LangGraph checkpoints, checkpoint anchors/outbox and resource authorization records are separate mechanisms; this change does not delete them.
- The checkpoint is the only Attempt execution state. No dual writes to an event stream, compatibility journal facade, or new generic effect layer.
- Keep every Attempt's final checkpoint. A later successful Attempt must not overwrite an earlier failed Attempt.
- Process-local workspace handles and clients are reconstructed on entry; they are not serialized.
- Handlers own commit timing. Save the external-call recovery state before dispatch; save output and related proof atomically; save preparation before promotion; save terminal proof before release; save release proof before reporting completion.
- External-call uncertainty still yields an indeterminate result. A promoted workspace is never converted into an uncommitted failure by a generic exception handler.

## Persisted model and storage

Introduce `AttemptCheckpoint` and a phase enum. A checkpoint holds Attempt identity, revision and fencing token; authorization; external activity identity, dispatch fingerprint, bound reference and observed result; workspace preparation/promotion proofs; terminal result; and system-interrupt records needed by the LangGraph bridge. Keep these fields typed and validate combinations before storage and on decode.

Use a full-record compare-and-set commit, not event append. The memory adapter and SQLite adapter implement the same interface. A commit atomically changes the complete checkpoint and increments its revision. Revision and live fence validation prevent stale updates even though process admission is exclusive. Canonical payload validation detects corrupted stored data.

Keep durable generation allocation and ownership on the store: latest generation, bounded registration, saved validated input, abandoned state and owner identity. These duties cannot disappear with the journal class.

SQLite stores one current checkpoint per Attempt and retains all distinct Attempts. Preserve generation and retained-call tables. New execution must not silently treat old nonempty journal-format databases as empty: reject unsupported old-format execution with a clear fresh-run instruction, preserve their bytes, and document that existing runs must be stopped with their original version before upgrading. No automatic legacy event-replay implementation is retained in the new runtime.

## Runtime and handlers

Use durable phases for authorization, execution, reconciliation, commit, termination, release and completion. A new Attempt starts at authorization. Dispatch commits the reconciliation phase and call identity before invoking the executor. A completed observation commits the commit phase together with its result. A failed result may proceed to termination, carrying the failure in the checkpoint. Prepared/promotion facts can be committed within the commit handler without inventing a separate phase for every internal function.

Runtime loads or creates a checkpoint, restores invocation-local prerequisites, and invokes a phase-to-handler mapping. Handlers return either durable progress or an existing final/pending/indeterminate resolution. Runtime reloads durable state after progress. Revision changes count as progress even when phase is unchanged. A handler that neither returns a resolution nor advances durable state is an error; waiting must yield and must not busy-loop. Durable completion can reconstruct the same result after restart.

Do not retain `AttemptAction`, `RuntimeProgress.completed`, `select_action`, or a second switch that reconstructs the old action decision under new names.

## Reader migration

1. All Attempt nodes: kernel, handlers, commit logic, factory and boot bindings use checkpoint storage.
2. Activity interface: OpenCode sees the existing typed activity view backed by checkpoint fields. Dispatch/bind mutations commit those fields and preserve live ownership checks.
3. Pending/resume bridge: issued, anchored and completed interrupt identities move into checkpoint fields. Human Flow gates continue using LangGraph state.
4. Retained host and force stop: validate the checkpoint's owner, authorization, activity reference and terminal/release status; cancel only the owned call. Keep the start gate closed when cancellation is unconfirmed.
5. Status and operator output: derive node state, external-session links, interruption statistics and output associations from checkpoint records.
6. Retro: keep its runtime-evidence port and artifact selection. Generate evidence from checkpoints, retaining failure fingerprints and proven subsequent recovery. Mark missing/incomplete evidence accurately. Replace the journal-specific evidence digest field with a checkpoint/source digest under an explicit schema version; do not label checkpoint bytes as journal bytes.

The full root currently mounts 39 business-step positions (38 unique contracts) and five human gates. The seven public roots cover 39 unique contracts; all six capability bundles cover 47. Business code has no direct Attempt journal reader. Retro's snapshot node reads a product projection; OpenCode uses the shared activity port. The test runner also supports activity mutation when such a port is supplied; preserve this interface.

## Verification and completion

Add focused tests before implementation for durable phase dispatch after reconstructing a runtime, full checkpoint CAS/fence/corruption, external-call recovery without duplicate dispatch, interrupted commit/promotion/release, pending/resume and generation budgets, retained foreground cancellation, status/output projection and Retro failure-success history. Replace journal-byte golden tests with meaningful checkpoint/recovery assertions; do not delete crash cases merely because their fixtures use events.

Run Ruff, formatting, Pyright, import contracts, full pytest and all three wheel smoke scripts on the final version. Inspect the package tree for obsolete Attempt journal/event/action imports. README and architecture documentation describe the new source of truth and the old-run limitation. No push, PR or merge is required by this request.
