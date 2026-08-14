# Benchmark Plan Remediation Design

## Context

Four OpenAI/OpenCode benchmark items stopped at a layer `codegen-precheck`. Each stopped review named `required_capabilities` entries that were syntactically rooted under C4 but did not resolve to typed leaves in `.aa/data-knowledge.yaml`. The downstream mechanical check detected the problem, but the benchmark resumed every interrupt with `accept_risk`; the precheck then correctly failed closed. The stopped child branch propagated to `assurance` immediately, so sibling layers waiting on shared resources never settled.

## Goals

- Reject typo/alias/scalar capability references at the review authoring boundary.
- Preserve legitimate knowledge gaps when the missing leaf is explicitly declared in the layer proposal.
- Give a reviewer a deterministic second pass with `*-plan-checks.json` after capability-key findings.
- Make benchmark interrupt decisions depend on the interrupt node and proposal state.
- Let all active assurance branches settle before the parent graph reports a business STOP.
- Preserve codegen prechecks and the overall fail-closed terminal result.

## Considered Approaches

### 1. Remove or weaken the precheck

Rejected. It would let codegen run with unresolved factories/auth/cleanup and turn a review error into unreliable tests.

### 2. Guess and normalize capability keys

Rejected. Mapping `factory_make_api` to `make_api` is obvious in this fixture but unsafe as a generic runtime rule. Nested entity constraint keys and invented aliases are even more ambiguous.

### 3. Validate references, rerun review, and settle siblings

Selected. A missing capability is valid only when it is a typed L1 leaf or is declared by the layer's L2 proposal. Mechanical findings route `fix_and_proceed` back through the reviewer, which now sees the generated checks. Benchmark automation chooses actions from the interrupt node instead of blindly accepting risk. At the assurance fan-out only, child STOP is captured as a settled branch result; an aggregate gate stops the parent after every active layer has settled.

## Design

### Capability-reference boundary

After registry/Pydantic validation of a `PlanReview`, finalization compares `required_capabilities` with the current L1 using `is_leaf_present`. Missing keys are accepted only when declared as typed leaves or `discovered_candidates[].knowledge_key` in `plans/data-knowledge.proposal.<layer>.yaml`. Undeclared missing keys return retryable `invalid_output` and name every offending key.

This preserves real knowledge-remediation semantics while rejecting symbol names, scalar constraint paths, and invented aliases before a gate can consume them.

### Two-pass review

The initial review may run without the mechanical checks artifact. Mechanical checks then validate the review. `knowledge-remediation -> fix_and_proceed` routes back to `review` for API, E2E, Fuzz, and Performance so a retry sees the exact `capability_keys` findings. Reviewer instructions explicitly state that checks are absent on the first pass and forbid using symbol values or scalar descendants as capability leaves.

### Benchmark action policy

`benchmark_interrupt_action(proposal_state, node_id)` returns:

- `fix_and_proceed` for `knowledge-remediation` without a pending proposal;
- `stop` for `knowledge-remediation` with a pending proposal, because L1 promotion is required;
- `accept_risk` for ordinary `human-review`, preserving benchmark's existing autonomous risk policy;
- `stop` for unknown interrupt nodes.

All Cursor/OpenCode loop entrypoints extract `node_id` from `aa status --json` and share this policy.

### Assurance branch settlement

`NodeDef.settle_child_stop` defaults to `false`. The four assurance layer nodes opt in. A stopped child freezes its partial workspace and returns a successful parent task value containing `child_status: stopped` and the reason. The existing `generation-join` therefore waits for all active branches. A new aggregate gate then emits STOP if any settled branch recorded a child STOP; otherwise execution continues unchanged.

This is scoped to the assurance fan-out. All other nested subgraphs retain the existing immediate STOP propagation behavior.

## Verification

- Unit tests for L1/proposal capability-reference validation.
- Shell-helper tests for interrupt action classification.
- Schema and integration tests for remediation routing and second-pass review.
- Runtime tests proving assurance settlement waits for siblings while ordinary child STOP still propagates.
- Packaged-schema compilation, targeted integration tests, and the repository CI gate.

