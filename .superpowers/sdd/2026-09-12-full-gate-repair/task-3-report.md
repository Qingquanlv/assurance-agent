# Task 3 report — Align Remaining Flat-Workspace Contracts

## Scope and root causes

- `execution_view.py` had retained durable `qa/tests/**` names *inside* the
  disposable execution view.  The approved contract projects durable
  `qa/tests/**` and `qa/fixtures/**` into one `tests/**` package in that view.
  Runner selection/authentication already has the required durable-to-view
  conversion in the committed baseline; restoring that alignment made the
  projected files executable without weakening durable path validation.
- The phase-4 handoff fixture and plan-consistency fixture used obsolete
  `tests/**` source paths.  `CodegenMapping` validates durable
  `qa/tests/**` targets, and planning facts only observe the actual durable
  source, so those inputs hid the intended contradiction.
- Agent-workspace tests still rejected approved flat output roots/paths,
  supplied outputs in non-canonical order, and declared a resource parameter
  no path interpolated.  `ResourceClaimTemplate` also still prohibited a
  parameter-free claim despite the flat layout having no dynamic change path;
  it now defaults to an empty mapping and continues to reject undeclared or
  unused parameters.
- The benchmark helpers still read the removed per-change execution/proposal
  tree.  They now use `qa/results/execution` and `qa/results/plans`, while
  preserving the existing `aa knowledge promote --change` CLI call.
- The claim-coverage test used the removed `qa/results/generated` seam rather
  than a real descendant relationship.
- The ignored local generated Cursor adapter directory was present and made
  the final benchmark manifest test fail.

## RED evidence

Initial command:

```console
uv run pytest --lf -q
```

Result: `16 failed in 1.43s`.

The failures comprised the deferred phase-3 manifest source-digest test plus
15 Task-3 failures: cross-wheel mapping, ignored Cursor adapter state, five
execution-view projection failures, two benchmark-loop helper failures,
claim coverage, four AgentWorkspace/resource-template checks, and planning
facts observation.

After the first minimal contract pass, the direct focused command was:

```console
uv run pytest -q tests/phase4/test_cross_wheel_contracts.py \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-generation/tests/test_plan_consistency.py \
  tests/product/test_execution_view.py tests/product/test_candidate_execution_isolation.py \
  tests/unit/benchmark/test_loop_helpers.py tests/phase6/test_final_benchmark_manifest.py
```

Result: `6 failed, 230 passed, 6 skipped`.  The six failures isolated the
remaining root causes: selector/authentication conversion in the execution
view and the model's now-obsolete nonempty resource-template-parameter rule.

## GREEN evidence

Directly affected regression group:

```console
uv run pytest -q tests/phase4/test_cross_wheel_contracts.py \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-generation/tests/test_plan_consistency.py \
  packages/capabilities/assurance-execution/tests/test_execution_view.py \
  packages/capabilities/assurance-execution/tests/test_runner.py \
  tests/product/test_execution_view.py tests/product/test_candidate_execution_isolation.py \
  tests/unit/benchmark/test_loop_helpers.py tests/phase6/test_final_benchmark_manifest.py
```

Result: `245 passed, 6 skipped in 10.63s`.

Post-format focused verification, including the graph-engine resource claim
tests:

```console
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_resource_arbiter.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/capabilities/assurance-execution/tests/test_runner.py \
  tests/phase4/test_cross_wheel_contracts.py \
  packages/capabilities/assurance-generation/tests/test_plan_consistency.py \
  tests/unit/benchmark/test_loop_helpers.py tests/phase6/test_final_benchmark_manifest.py
```

Result: `225 passed, 6 skipped in 9.63s`.

Focused Python lint/format:

```console
uv run ruff check <Task-3 Python files>
uv run ruff format --check <Task-3 Python files>
```

Result: `All checks passed!`; format check passed after formatting
`tests/phase4/cross_wheel.py`.

Required final last-failure check:

```console
uv run pytest --lf -q
```

Result: exactly one expected failure:
`tests/agent_runtime/test_phase3_acceptance_artifacts.py::test_committed_live_manifest_pins_the_required_release_fields`.
The committed phase-3 manifest and its source digest were not changed.

## Files changed

- `benchmark/vue-fastapi-admin/benchmark/loop-helpers.sh`
- `packages/framework/graph-engine/graph_engine/plugin_api.py`
- `packages/capabilities/assurance-execution/assurance_execution/execution_view.py`
- `packages/capabilities/assurance-execution/assurance_execution/operations/runner.py`
- `packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`
- `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- `packages/capabilities/assurance-execution/tests/test_runner.py`
- `packages/capabilities/assurance-generation/tests/test_plan_consistency.py`
- `tests/phase4/cross_wheel.py`
- this report

`operations/paths.py` was inspected and restored to its existing correct
durable-to-view selector conversion; it has no net diff and is not staged.

## Generated-state backup

The ignored generated directory was moved intact from
`packages/adapters/agent-runtime-cursor/` to:

```text
/private/tmp/assurance-gate-repair.GBlnmT/agent-runtime-cursor
```

## Self-review

- Durable authority remains `qa/tests/**`; only the disposable execution
  projection uses `tests/**`.
- Canonical ordering, duplicate rejection, and path-escape rejection remain
  covered by the AgentWorkspace tests.
- Parameterized resource claims still require every declared parameter to
  appear in a claim path; only parameter-free claims are newly allowed.
- No phase-3 manifest/source-digest field was changed.
- No `.superpowers/brainstorm`, `.vscode`, example runtime state, or unrelated
  SDD scratch file is staged.

## Concerns

`git diff --check` over the whole worktree reports an unrelated pre-existing
trailing blank line in `.superpowers/sdd/progress.md`; that file is neither
changed nor staged by this task.  Several Task-3 files were already dirty as
part of the uncommitted flat-workspace feature, so staging them includes their
complete feature diff, not solely the small Task-3 hunks.
