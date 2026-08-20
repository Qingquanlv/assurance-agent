# Task 12 report — Phase 1 acceptance gate

## Status

Task 12 implementation and the scoped Phase 1 acceptance gate are complete at
`4ab7b35c7244cf65282c5220b79943ab5ccf5d5b`. The only non-green repository-wide
command is `uv run pytest -q`, whose four failures exactly reproduce the
user-approved clean-worktree benchmark scaffold baseline. No ignored artifacts
were fabricated and no unrelated benchmark behavior was changed.

## Delivered

- Added the product-required `python -m graph_engine` `compile` and `run`
  commands with deterministic JSON evidence.
- Kept product/plugin entry-point loading named and invocation-scoped. `run`
  requires an explicit plugin list and rejects missing or extra plugin IDs.
- Kept `Engine`'s missing-host behavior unchanged and fail-closed. The CLI
  constructs its own narrowly documented, trusted-wheel-only, in-process host
  at the `run` command boundary.
- Added both the syntax-tree import firewall and the import-linter forbidden
  contract.
- Added an offline smoke that archives committed `HEAD`, builds and inspects
  engine/toy wheels, and exercises three isolated venvs.
- Added CI wiring after the existing packaging smoke, documented Phase 1's
  public interfaces/node kinds/trust boundary, and closed the pre-existing
  Ruff format drift in `plugin_api.py`.

## RED → GREEN

Initial exact command:

```text
uv run pytest packages/graph-engine/tests/test_cli.py tests/architecture/test_graph_engine_boundaries.py -v
```

RED output: `3 failed, 1 passed, 1 warning in 0.72s`. All three CLI tests failed
because Python reported `No module named graph_engine.__main__`; the independent
AST firewall already passed.

After implementation, the same command reported:

```text
4 passed, 1 warning in 0.87s
```

The committed-archive smoke then produced three meaningful RED corrections:

1. `assurance_agent` was visible from the archived source working directory;
   fixed by leaving the source tree before installed-wheel imports.
2. macOS returned `/var/...` through `mktemp`, which the Engine correctly
   rejected as a symlinked namespace; fixed by canonicalizing with `pwd -P`.
3. immutable snapshot files made cleanup exit 1 after functional success;
   fixed by restoring owner write permission only inside the validated temp
   directory before removal.

Final smoke exit: `0`; final line: `graph-engine smoke test: OK`.

## Commits

Task 12 scoped commits, in order:

1. `b4939b1a1240d39c5f576fa1177c38c88f8d8594` — `build(graph-engine): enforce phase one isolation gates`
2. `26186483c46faf2d757841684c9d4b902e5b8d91` — `fix(graph-engine): isolate smoke imports from source`
3. `6ed95a01931415b0f810ce6a73c135384bc5dab5` — `fix(graph-engine): canonicalize smoke workspace`
4. `b2980dab4e8407a994be8ce079ba10d62b352262` — `fix(graph-engine): clean immutable smoke trees`
5. `4ab7b35c7244cf65282c5220b79943ab5ccf5d5b` — `fix(graph-engine): use pinned smoke interpreter`

The twelve task endpoint commits, in task order:

1. Task 1 — `3a81de84d983b6aabcefe568dd1328d8f8ca7f0a`
2. Task 2 — `99e83ea9b016a7b347265c349143d9e8acefd076`
3. Task 3 — `66aafb3b59a07497efdeea700e27c369af2b5827`
4. Task 4 — `cc6bf3adbb3c730acdfedd4fde27b1a720683789`
5. Task 5 — `6a0b819ac6d11fc3ffd7d985ff654d6ff8cbf6b9`
6. Task 6 — `8c0fe6015e333f5ab5aa2346ce5d9c365856ce2e`
7. Task 7 — `178143ee71e8ec4a2c80fde697f123146a2377d5`
8. Task 8 — `916f31465aed9606feefffa09cf9db7012c8bd1a`
9. Task 9 — `49cd5741123ef83554c3cfcc94e8b73510ad9856`
10. Task 10 — `f5b2cdcb0f96df5ab3227bb72fe27b27d383843e`
11. Task 11 — `0204c094cec34832f2b549656ac89ffef89471ef`
12. Task 12 — `4ab7b35c7244cf65282c5220b79943ab5ccf5d5b`

## Exact focused gate evidence

```text
uv run ruff check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
All checks passed!

uv run ruff format --check packages/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
40 files already formatted

uv run pyright packages/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b
0 errors, 0 warnings, 0 informations

uv run lint-imports
Analyzed 632 files, 2153 dependencies.
Contracts: 12 kept, 0 broken.

uv run pytest packages/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py -q
562 passed, 1 skipped, 1 warning in 6.47s

bash scripts/graph_engine_smoke_test.sh
graph-engine smoke test: OK
```

The new import contract was explicitly reported as
`graph engine must not import products or the old runtime KEPT`.

## Wheel and toy evidence

Built engine wheel file list:

```text
graph_engine-0.1.0.dist-info/METADATA
graph_engine-0.1.0.dist-info/RECORD
graph_engine-0.1.0.dist-info/WHEEL
graph_engine/__init__.py
graph_engine/__main__.py
graph_engine/canonical.py
graph_engine/errors.py
graph_engine/graph/__init__.py
graph_engine/graph/compiler.py
graph_engine/graph/expressions.py
graph_engine/graph/schema.py
graph_engine/identifiers.py
graph_engine/plugin_api.py
graph_engine/product.py
graph_engine/runtime/__init__.py
graph_engine/runtime/checkpoint.py
graph_engine/runtime/engine.py
graph_engine/runtime/events.py
graph_engine/runtime/frozen_json.py
graph_engine/runtime/ledger.py
graph_engine/runtime/models.py
graph_engine/runtime/planner.py
graph_engine/runtime/scheduler.py
graph_engine/runtime/workspace.py
```

Toy A terminal evidence from the final committed-HEAD smoke:

```json
{"compiled_digest":"a2816f61a550dd6559e9ddbb4e828a6bb2022746ce7a05dc88169dde874adf1b","final_tree_id":"017677d082d26382926052ddd44561c916e79178683b9955db72cd942d05fe47","ledger_digest":"cccb60de5f45c15a73ebd5d63adb28c0001796b7ecba5a79cfb5b2fa618899b3","product_digest":"be0f47184c551337c11bfe73e0908f912f8b1024dbf4396ff4994d55bc51945b","status":"succeeded"}
```

Toy B interrupt/resume evidence from that smoke:

```json
{"blocked_status":"interrupted","compiled_digest":"3597f9a71d2fa1a1ba1631bdae157c28c1bd67a535f7f86ea0977708cd9e4084","final_tree_id":"e6283a93db6c6f286a4fe87e39e71f5d238a9d40c1072b32852bf1cd72f4189b","ledger_digest":"a3c77ca8699ee536e77fa9069326c454ff839ed77e2f9ebe458656095eaced8e","product_digest":"c5ec4e9308ec659a6fa18edc6529a2efa6dcd7fcdcdb8a5e0a126444d4d9af57","terminal_status":"succeeded"}
```

The smoke also verified the engine wheel has only `graph_engine/**` and
distribution metadata, no YAML/JSON/Markdown resources, no Assurance/toy
import package, and neither toy wheel depends on `assurance-agent` or
`assurance-kernel`.

## Repository-wide gate evidence

```text
uv run ruff check .
All checks passed!

uv run ruff format --check .
1131 files already formatted

uv run pyright
0 errors, 0 warnings, 0 informations

uv run lint-imports
Analyzed 632 files, 2153 dependencies.
Contracts: 12 kept, 0 broken.

uv run pytest -q
4 failed, 7305 passed, 13 skipped, 1 warning in 695.62s (0:11:35)

bash scripts/packaging_smoke_test.sh
packaging smoke test: OK
```

### Approved baseline disposition

The exact four failures are:

1. `test_benchmark_test_scaffold_is_an_importable_package`
2. `test_shared_e2e_login_uses_locators_present_in_the_real_dom`
3. `test_shared_fuzz_fixtures_use_the_live_sut_without_invented_app_imports`
4. `test_generated_http_scaffold_matches_observed_response_shapes`

They remain an unrelated clean-worktree scaffold gap in
`tests/unit/benchmark/test_opencode_openai_loop.py`: the assertions/read calls
target missing files below `benchmark/vue-fastapi-admin/tests/`.
`git check-ignore -v` maps every representative missing path to `.gitignore:31`
(`benchmark/vue-fastapi-admin/tests/*`), and `git ls-files --error-unmatch`
confirms none is tracked. This exactly matches the ledger baseline; it is not a
tracked packaging/test defect within Task 12 scope.

## Deferred-minor audit

Task 4's `EntryPoint.load()` minor was already closed before Task 12. Fresh
verification:

```text
uv run pytest packages/graph-engine/tests/test_product_resolution.py -q -k product_entrypoint_load_failure_is_normalized
1 passed, 32 deselected in 0.12s
```

The test proves the selected entry-point load error becomes
`ProductResolutionError` and preserves the original exception as `__cause__`.

## Deviations and contract tension

- The exact plan command `compile --product toy-a` has no `--plugin` argument.
  The two Phase 1 toys intentionally publish their product and sole plugin
  under the same explicit entry-point name, so compile loads that exact named
  pair. `run` does not infer: it requires repeatable explicit `--plugin` values
  and enforces equality with the manifest requirements.
- `TaskExecutionHost` documents a real production confinement boundary, while
  Phase 1's architecture explicitly excludes hostile-wheel sandboxing. The CLI
  adapter is therefore in-process and does not claim confinement; its name,
  docstring, command-boundary construction, and README scope it to explicitly
  selected trusted Phase 1 wheel demonstrations. `Engine` gained no default
  host, and the existing missing-host test remains in the 562-test focused gate.
- The repository-wide pytest expectation cannot honestly be reported green.
  The exact approved baseline was preserved and re-audited instead of being
  hidden, skipped, or repaired with ignored generated files.

## Self-review

- Standards axis: the first independent review found one hard AGENTS.md
  violation (`python3` instead of pinned uv-managed Python). Commit `4ab7b35`
  corrected it; re-review reports the finding addressed and no new standards
  or smell findings.
- Spec axis: independent re-review against the Task 12 brief, Phase 1 global
  constraints, and controller additions reports no findings.
- Manual mutation review: removing `__main__`, weakening product requirement,
  bypassing the explicit plugin set, importing a product package, leaking the
  archived source directory, adding a forbidden wheel resource/package, or
  omitting toy terminal evidence is covered by a CLI/AST/smoke assertion.
- Scope review: only the eight Task 12 implementation paths and this report
  changed; `progress.md` was not modified.

## Concerns

The only open engineering concern is the explicit trust-boundary limitation:
the demonstration CLI host executes reviewed wheels in-process and must not be
presented as a production confinement mechanism. A real out-of-process/OS
capability host remains future work, consistent with the architecture spec.
