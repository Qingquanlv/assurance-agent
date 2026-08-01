# Task 3 Report — Typed Compiler Diagnostics and Dark-Ship Current Conformance

## Status

**DONE**

No git commit (controller owns commit policy). Left `cursor-loop-helpers*` alone.
Packaged YAML/contracts were not activated.

## What Was Implemented

### Typed compiler diagnostics

- `CompileDiagnostic` + `CompileDiagnosticCategory` in `compiler.py`
- `CompileError.diagnostics`: sorted immutable `tuple[CompileDiagnostic, ...]`
- `str(exc)` still deterministic; historical identity failures attach
  `historical_ingest_identity` / `historical_contract_identity` diagnostics
- Generic batch compile failures attach `workflow_validation` diagnostics
- `definition_pinning._pinned_reason_for_compile_error` switches on
  category/code first (substring fallback retained for bare string errors)

### Topology analysis (pure helpers)

New `assurance_agent/workflow/graph/topology_analysis.py`:

- CFG build over ordinary edges + route outcomes
- reachability / dominance / avoid-path helpers
- finite selection-domain generator and truth-table predicate equivalence
- unknown params/builtins and parse failures count as mismatch
- test helpers: commutative `and` / list-literal reorder
- AST helpers: `expressions_structurally_equal` (order-insensitive `in` lists)
  and `expression_has_top_level_predicates` for required-atom enforcement

### Dark-ship current assurance conformance

New `assurance_agent/workflow/graph/assurance_conformance.py`:

- `AssuranceConformanceIssue` + `AssuranceConformanceCode`
- `find_current_assurance_conformance_issues(schema)` — strict, sorted, no mutation
- `with_approved_api_e2e_capability_atoms(schema)` — test-only model_copy target
  (API/E2E codegen capability atoms + plan-gate policy-floor atoms)
- `render_assurance_conformance_issues(...)` string helper

Strict checks cover §8.7 corpus: selection truth table, run modes, specialty
preflight guards, role uniqueness, evidence reads/aliases, fail-closed gate
atoms/precedence, bypass edges/routes, interrupt bind/checkpoint/actions/
allowlist, remediation returns, generation join.

Plan-gate rules use top-level AST required-atom enforcement (not substring-only):
`skip_when` / `reject_when` / `pass_when` / `stop_when`, plus
`missing_field_is` / `missing_file_is` fail-closed stop. Generation-join
successors must be truth-equivalent to `params.run_tests == true|false`.

### Compatibility boundary (no activation)

- `validate_current_assurance_activation(...)` remains the wired-classification
  string path used by `compile_packaged_workflow`
- Strict validator is **not** invoked from `compile_packaged_workflow` (Task 15)
- Unmodified packaged schema: compatibility validator passes; strict validator
  reports missing API/E2E codegen capability atoms
- Approved model_copy target: strict validator returns `()`

## Review Fix Pass (Important findings)

### Important #1 — Plan-gate atom weakenings must fail

`_plan_gate_issues` now rejects (stable `gate_rule_mismatch`) on the approved
target:

- `skip_when` / `reject_when` weakened to `false`
- `pass_when` missing `decision == 'pass'`, `codegen_readiness`, or policy-floor
  top-level AND atoms
- `missing_field_is` / declared `missing_file_is` changed away from stop

Mutation tests assert non-empty issues with locator-stable codes. Enforcement
uses closed-domain / top-level AST required predicates (§8.7), not substring
probes.

### Important #2 — Generation-join guarded successors

Misrouting `generation-join -> execution` with `when: "true"` (or any guard
not truth-equivalent to `params.run_tests == true`) yields
`generation_join_mismatch`. Covered by
`test_generation_join_guarded_successor_misrouted_rejected`.

### Also cleaned while touching those areas

- Removed tautological plan-gate route `code` ternary (always
  `route_case_mismatch`)
- Replaced no-op codegen `stop_when` branch with required OR-atom check for
  `node('<cycle>').status != 'succeeded'`
- `test_broadened_specialty_preflight_rejected` now asserts
  `run_mode_predicate_mismatch`

## Review Fix Pass (post-fa0ba99)

Prior Importants remain closed. New findings:

### P1 — Empty gate-rule expressions

`expression_has_top_level_predicates` treats empty/whitespace as mismatch.
Plan-gate and codegen checks run whenever the field is present (`if field in
rules and not ...`), so `""` / `"   "` yield:

- plan-gate → `gate_rule_mismatch` on `.skip_when` / `.reject_when` /
  `.pass_when` / `.stop_when`
- codegen → `codegen_precondition_mismatch` on the same fields

### P1 — OR-rule broadening (`… or true`)

`expression_has_top_level_predicates(..., exact=True)` requires the top-level
OR operand set to equal the closed required atom set (no extra disjuncts):

- plan `stop_when`: exactly invalid `plan_assurance_state` atom
- plan `reject_when`: closed `{decision==reject, readiness==not_ready, policy-block}`
- codegen `stop_when`: closed `{child status != succeeded, invalid state, not file_exists(L1)}`

### P2 — Codegen `pass_when` structural atoms

Codegen `pass_when` / `skip_when` / `stop_when` use the same AST top-level
predicate helper (not substring). Spoof nesting
`(capabilities_present(...) or true)` is rejected as
`codegen_precondition_mismatch`.

## Verification

| Command | Result |
|---|---|
| Focused pytest (compiler + mutations + packaged + runtime factory) | **124 passed** |
| `uv run ruff check` on touched graph/conformance/test files | All checks passed |

## Files Changed

```
assurance_agent/workflow/graph/topology_analysis.py          (new + AST helpers)
assurance_agent/workflow/graph/assurance_conformance.py      (new + review tightenings)
assurance_agent/workflow/graph/compiler.py                   (modified)
assurance_agent/workflow/graph/definition_pinning.py         (modified)
assurance_agent/workflow/graph/replay_schema.py              (modified)
tests/unit/workflow/graph/test_compiler.py                   (modified)
tests/unit/workflow/graph/test_assurance_topology_mutations.py (new + review mutations)
tests/unit/workflow/graph/test_packaged_schema_compiles.py   (modified)
.superpowers/sdd/task-3-report.md                            (this report)
```

## Commit

Not created. Suggested message when controller stages:

```
feat(graph): add typed assurance conformance diagnostics
```

## Concerns

- Specialty `needs_fix` routing assumes the packaged human-review shape;
  API/E2E still expect an automatic fixer node named via skill discovery.
- Policy-floor atoms for non-API plan gates are injected only on the approved
  dark-ship target until packaged YAML activation.
