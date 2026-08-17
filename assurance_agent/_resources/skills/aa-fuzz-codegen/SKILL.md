---
name: aa-fuzz-codegen
description: Generate fuzz tests and a strict generated-files manifest after the plan gate passes.
---

## Purpose

Generate fuzz tests under `tests/fuzz/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. The graph gate is the progression authority.

## Inputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-codegen-mapping.yaml`
- `change:plans/fuzz-review-summary.md`
- `change:review/fuzz-plan-review.json`
- `change:review/fuzz-plan-checks.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/fuzz/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/fuzz-codegen-summary.md`
- `change:codegen/fuzz-generated-files.json`

### conditional

- `repo:tests/fuzz/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source.

Before writing the manifest, verify every top-level `tests.*` import and every
dynamic `*_MODULE = "tests...."` reference resolves to either a frozen repository
file or a file generated in this candidate. A data-knowledge symbol is not proof
that its Python module exists. Generate the authorized support module or use an
existing resolvable implementation; never defer a missing local module to pytest.

Only `test_entry` manifest entries may claim mapped Case IDs, and their
`case_ids` must exactly match the codegen mapping (`plans/fuzz-codegen-mapping.yaml`) for that path. Support and
shared-builder entries always use `case_ids: []`.

Use `generated` only for a newly added file and `updated` only for a file whose
content this invocation changed. `reused` is legal only for an unchanged,
selected private-root `test_entry` that is itself a codegen-mapping target. Never
list an unchanged adapter, helper, fixture, or shared builder as `reused`; omit
unchanged support dependencies from the manifest.

When Hypothesis tests also accept pytest fixtures, bind generated values by name
(`@given(field=...)`), never positionally. Positional strategies bind from the
right and can make pytest treat the intended generated parameter as a fixture.

Implement every mapped test with the exact canonical symbol
`test_<case_id_lowercase>__<behavior>` so the test-tree scanner can recover the
complete Case ID. Before finishing, verify each selected Case ID appears in its
generated function name.

Do not bind different imports to the same module-level name. In particular,
alias Hypothesis configuration (for example
`from hypothesis import settings as hypothesis_settings`) when the test also
imports repository settings such as `from tests.config import settings as
qa_settings`.
