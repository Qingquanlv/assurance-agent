# Task 2 — Phase 2 Tail Behavioral Reconciliation

Status: complete.

## Method

Inspected `916b68d`, `e1ada99`, `36eafa1`, `f6f9098`, `3bfe0c`, and `fc50036`
without cherry-picking. `phase2-equivalence.json` maps each retained invariant to
a current behavioral test node and records whether the current engine was already
equivalent or required a minimal port.

## Findings

- `916b68d`: equivalent. The current durable ledger plus
  `test_executor_reconciles_after_apply_started_without_blind_reapply` preserves
  recovery by reconciliation rather than blind re-application.
- `e1ada99`: equivalent. Retry exhaustion remains terminal and non-retryable in
  `test_executor_exhausts_policy_as_non_retryable_failure`.
- `36eafa1`: equivalent. The current authenticated wheel loader rejects an
  arbitrary preloaded module at an authenticated source location.
- `f6f9098`: equivalent. Explicit installed-wheel product/plugin execution is
  retained; the installed-wheel smoke also passed.
- `3bfe0c`: equivalent at the product-neutral effect seam. Toy A no longer owns
  Phase 2 effect business semantics, so the generic durable effect recovery node
  is the behavioral replacement; no Toy A behavior was restored.
- `fc50036`: ported. Runtime matching and `SchemaEntry` construction now reject
  unknown JSON Schema keywords recursively, so unsupported schemas fail before
  execution or registry admission.

No SnapshotStore, workspace tree/HEAD state, whole-tree export, Assurance
meaning, project-loaded extension point, removed Change-local module, or
historical conformance module was restored.

## Verification

- `uv run pytest tests/phase6/test_phase2_invariant_reconciliation.py packages/graph-engine/tests/runtime/test_effects.py packages/graph-engine/tests/composition -q` — 408 passed.
- `bash scripts/graph_engine_smoke_test.sh` — passed after the required
  unsandboxed retry for uv's local cache.

The brief's literal focused command also names the deleted
`packages/graph-engine/tests/runtime/test_json_schema.py`; it predictably stops
with “file or directory not found”. The Phase 6 reconciliation test is the
replacement schema coverage, and the equivalent runnable focused command above
is green.

## Fix Round 1 — Preserve Declared Dialect Compatibility

Removed the unrequested `$schema` value restriction from the runtime and
`SchemaEntry` closed-keyword validators. They continue to require a text
`$schema` value and reject unknown keywords recursively.

Regression red command:

```console
uv run pytest tests/phase6/test_phase2_invariant_reconciliation.py::test_closed_schema_accepts_declared_standard_dialects -q
```

Regression red output:

```console
FAILED tests/phase6/test_phase2_invariant_reconciliation.py::test_closed_schema_accepts_declared_standard_dialects
ValueError: unsupported schema dialect: 'https://json-schema.org/draft/2020-12/schema'
```

Green verification command:

```console
uv run pytest tests/phase6/test_phase2_invariant_reconciliation.py packages/graph-engine/tests/composition/test_registries.py packages/graph-engine/tests/runtime/test_effects.py -q
uv run ruff check packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py tests/phase6/test_phase2_invariant_reconciliation.py
uv run ruff format --check packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py tests/phase6/test_phase2_invariant_reconciliation.py
uv run pyright packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py tests/phase6/test_phase2_invariant_reconciliation.py
```

Green verification output:

```console
54 passed, 1 warning in 5.69s
All checks passed!
3 files already formatted
0 errors, 0 warnings, 0 informations
```

## Reopened Fix Round 2 — Scope the Closed Schema Boundary to Durable Effects

### Root cause

Task 2 commit `e1960ec` made every `SchemaEntry` use the runtime's deliberately
closed JSON-Schema vocabulary. That made legitimate rich product/result schemas
fail during contribution composition, before any provider call. The fresh live
failure stopped first at
`assurance.intake.schema.case-authoring.v1`, but moving the boundary exposed that
the installed Healing and Improvement effect schemas also required a larger,
still-finite runtime vocabulary.

The corrected ownership boundary is:

- `SchemaEntry` authenticates JSON object/boolean content, duplicate keys,
  non-finite constants, media type, digest, and textual `$schema`, while accepting
  ordinary rich product schemas.
- `RegistrySet` validates only schemas referenced by an `EffectEntry` as intent or
  receipt against the closed runtime subset before the registry can be published.
- direct runtime schema matching uses the same shared validator, so composition
  and effect execution cannot drift.

### Machine-audited effect keyword set

The installed Assurance wheel composition test walks every schema referenced by
an `EffectRegistration` and compares its schema-keyword inventory to this exact
set:

```text
$defs, $ref, additionalProperties, anyOf, const, default, enum, items,
minItems, minLength, minimum, pattern, properties, required, title, type
```

`title` and `default` are explicit annotations with no assertion semantics. The
validation/applicator keywords have implemented runtime behavior. `$ref` is
limited to one-segment local `#/$defs/<name>` references with JSON Pointer
escaping; external, missing, nested, malformed, and cyclic references fail
closed. The previously supported textual `$schema` keyword remains accepted.
Unimplemented keywords such as `format` remain rejected both during effect
registry composition and direct runtime use.

### RED evidence

The initial boundary regression command was:

```console
uv run pytest -q \
  tests/phase6/test_phase2_invariant_reconciliation.py::test_product_schema_entry_accepts_standard_rich_keywords \
  packages/graph-engine/tests/composition/test_registries.py::test_registry_rejects_unsupported_keyword_only_for_effect_schema \
  tests/phase5/test_cli_compile.py::test_installed_assurance_contributions_accept_rich_product_schemas
```

It failed `3` tests: rich `SchemaEntry` construction rejected `$defs`, the
effect-only test failed too early as generic invalid schema content, and the
installed Assurance composition reproduced the live
`invalid schema content: assurance.intake.schema.case-authoring.v1` failure.

After moving the boundary, the first two tests passed and installed composition
failed on `assurance.healing.schema.allocation-intent.v2` with
`unsupported schema keyword: title`. That proved the actual effect schema set
had to be inventoried and supported rather than bypassed or rewritten.

The runtime vocabulary RED command then produced `13 failed, 2 passed`, covering
the installed keyword inventory, local reference safety, `anyOf`, and concrete
string/number/array constraint semantics.

### GREEN evidence

Focused graph/runtime/composition plus installed Assurance wheel composition:

```console
uv run pytest -q \
  tests/phase6/test_phase2_invariant_reconciliation.py \
  packages/graph-engine/tests/runtime/test_effects.py \
  packages/graph-engine/tests/runtime/test_json_schema.py \
  packages/graph-engine/tests/composition \
  tests/phase5/test_cli_compile.py::test_installed_assurance_contributions_accept_rich_product_schemas \
  tests/phase5/test_cli_compile.py::test_installed_assurance_effect_schemas_use_the_audited_closed_keyword_set
```

Result: `427 passed, 1 warning`.

Targeted lint, format, and type checking all passed:

```console
uv run ruff check packages/graph-engine/graph_engine/json_schema.py packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py packages/graph-engine/graph_engine/composition/registries.py packages/graph-engine/tests/runtime/test_json_schema.py packages/graph-engine/tests/composition/test_registries.py tests/phase5/test_cli_compile.py tests/phase6/test_phase2_invariant_reconciliation.py
uv run ruff format --check packages/graph-engine/graph_engine/json_schema.py packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py packages/graph-engine/graph_engine/composition/registries.py packages/graph-engine/tests/runtime/test_json_schema.py packages/graph-engine/tests/composition/test_registries.py tests/phase5/test_cli_compile.py tests/phase6/test_phase2_invariant_reconciliation.py
uv run pyright packages/graph-engine/graph_engine/json_schema.py packages/graph-engine/graph_engine/runtime/json_schema.py packages/graph-engine/graph_engine/composition/models.py packages/graph-engine/graph_engine/composition/registries.py packages/graph-engine/tests/runtime/test_json_schema.py tests/phase5/test_cli_compile.py tests/phase6/test_phase2_invariant_reconciliation.py
```

The full `tests/phase5/test_cli_compile.py` file also ran as part of a broader
trial: `430 passed`, while two pre-existing CLI audit assertions failed after
successful composition because the dirty graph inventory lacks
`full/achieved`, `full/improvement`, and `full/retro`. This fix does not modify
that unrelated inventory.

Post-commit installed-wheel verification:

```console
bash scripts/graph_engine_smoke_test.sh
```

Result: `graph-engine smoke test: OK`. The emitted wheel inventory explicitly
contains `graph_engine/json_schema.py`, and isolated Toy A/Toy B execution
remains terminally successful.
