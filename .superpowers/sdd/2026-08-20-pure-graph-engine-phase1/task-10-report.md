# Task 10 report — Toy product A

## Scope

Added the independently packaged `graph-engine-toy-a` workspace wheel.  It
registers `toy-a` entry points in `graph_engine.products` and
`graph_engine.plugins`, provides a sequential `greet -> done` workflow, and
executes `toy.a.greet` using only `graph-engine` APIs.

`load_plugin_entrypoint()` now discovers exactly one plugin in the dedicated
entry-point group without global registration.  Product and plugin entry-point
loading share the same fail-closed selection helper.  A selected entry point
whose `load()` fails now raises `ProductResolutionError` with the original
exception as its cause.

## RED / GREEN

1. RED — created `packages/graph-engine/tests/integration/test_toy_a.py` and
   ran:

   ```console
   uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v
   ```

   Initially collection failed because `load_plugin_entrypoint` did not exist.
   After adding its unit-test contract and implementation, the integration test
   failed closed as intended with `ProductResolutionError: no product entry
   point for toy-a`.

2. GREEN — added the wheel, workspace registration, product, plugin, and
   explicit test-only host.  The first executable run exposed two genuine
   integration mismatches: entry-point targets are loaded as classes (so the
   stateless provider methods must be static) and a node's declared input is
   wrapped as `request.input["config"]`.  The final manifest declares
   `{"name": "Ada"}`, allowing the required handler read at
   `request.input["config"]["name"]`.

3. Regression RED/GREEN — added a selected-entry-point `ImportError` test.
   It was red before the centralized loader wrapped `EntryPoint.load()`; it is
   green with `ProductResolutionError` preserving `__cause__`.  Added plugin
   group, unknown, and duplicate tests alongside it.

## Verification

All commands were run from the Task 10 worktree.

```console
uv sync --dev
# Resolved 43 packages; audited 41 packages; exit 0.

uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v
# 1 passed in 0.23s.

uv run python -c "import graph_engine_toy_a; import graph_engine; assert 'assurance_agent' not in __import__('sys').modules"
# exit 0.

uv run pytest packages/graph-engine/tests/test_product_resolution.py -v
# 33 passed in 0.11s.

uv run pytest packages/graph-engine/tests -v
# 553 passed, 1 skipped in 5.62s.

uv run ruff check packages/graph-engine/graph_engine/__init__.py packages/graph-engine/graph_engine/product.py packages/graph-engine/tests/test_product_resolution.py packages/graph-engine/tests/integration/test_toy_a.py examples/graph-engine-toy-a
# All checks passed.

uv run ruff format --check packages/graph-engine/graph_engine/__init__.py packages/graph-engine/graph_engine/product.py packages/graph-engine/tests/test_product_resolution.py packages/graph-engine/tests/integration/test_toy_a.py examples/graph-engine-toy-a
# 7 files already formatted.

uv run pyright packages/graph-engine/graph_engine examples/graph-engine-toy-a packages/graph-engine/tests/test_product_resolution.py packages/graph-engine/tests/integration/test_toy_a.py
# 0 errors, 0 warnings, 0 informations.

git diff --check
# exit 0.
```

## Self-review

- The engine retains its explicit `TaskExecutionHost` boundary.  The sole
  in-process host is an integration-test double and is named/documented as
  unconfined and non-production.
- Engine, invocation handle, and workspace stores use deterministic context
  lifecycles in the integration test.
- Entry-point discovery looks only in the requested group, rejects zero or
  multiple matches before calling `load()`, and does not mutate a registry.
- The toy wheel depends only on `graph-engine`; the isolation import assertion
  confirms importing the wheel and engine does not import `assurance_agent`.
- No default product, graph, plugin, or unsafe runtime host was added.

## Deviation from the illustrative snippet

The illustrative `Engine(tmp_path / "engine")` is intentionally replaced with
an explicit `_InProcessTestHost`, preserving the established fail-closed
production-host rule.  `ToyAProduct` and `ToyAPlugin` are stateless class entry
points with static protocol methods because Python entry-point loading returns
the declared class rather than constructing it.  This leaves registration
invocation-scoped and introduces no default instance or registry.

## Commit

`ab91ca3` — `test(graph-engine): add sequential toy product`

## Review correction — executable one-retry policy

The Task 10 review found that the initial `max_attempts: 1` declaration and
empty default `retry_on` tuple made every first failure terminal.  The planner
counts the first execution as attempt 1 and permits another attempt only when
the failure kind is in `retry_on` and the current attempt remains below
`max_attempts`.

### RED / GREEN

Added `test_toy_a_retries_a_transient_first_greet_attempt`, using the existing
explicit, test-only `_InProcessTestHost` to return
`TaskOutcome.failed("transient", ...)` for its first Toy A execution.  RED:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v
# 1 passed, 1 failed. The new test ended as
# task_failed:greet:transient after attempt 1.
```

GREEN changes only the Toy A policy to
`max_attempts: 2, retry_on: [transient]`; the production `toy.a.greet` handler
is unchanged.  The integration assertion observes exactly attempts `(1, 2)`,
the first attempt's `transient` failure, the second success, final
`{"message": "hello Ada"}` output, and `greeting.txt` content.

### Correction gates

```console
uv sync --dev
# Resolved 43 packages; audited 41 packages; exit 0.

uv run pytest packages/graph-engine/tests/integration/test_toy_a.py -v
# 2 passed in 0.26s.

uv run python -c "import graph_engine_toy_a; import graph_engine; assert 'assurance_agent' not in __import__('sys').modules"
# exit 0.

uv run pytest packages/graph-engine/tests/test_product_resolution.py -v
# 33 passed in 0.11s.

uv run pytest packages/graph-engine/tests -v
# 554 passed, 1 skipped in 5.68s.

uv run ruff check examples/graph-engine-toy-a packages/graph-engine/tests/integration/test_toy_a.py
# All checks passed.

uv run ruff format --check examples/graph-engine-toy-a packages/graph-engine/tests/integration/test_toy_a.py
# 4 files already formatted.

uv run pyright examples/graph-engine-toy-a packages/graph-engine/tests/integration/test_toy_a.py
# 0 errors, 0 warnings, 0 informations.

git diff --check
# exit 0.
```

Correction commit: `93f9174` — `fix(graph-engine): enable toy product retry`.
