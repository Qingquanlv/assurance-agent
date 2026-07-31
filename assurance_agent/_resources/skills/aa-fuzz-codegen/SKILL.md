---
name: aa-fuzz-codegen
description: Generate schemathesis fuzz tests from a reviewed fuzz plan after the graph gate has passed. Reads exact plan, review, checks, cases, config, and L1 inputs. Does not execute pytest.
---

## Purpose

Translate a reviewed fuzz plan into executable schemathesis tests. This skill does **not** execute pytest and does **not** decide workflow progression — the graph gate is the progression authority.

Apply non-deprecated guidance from `.aa/memory/aa-fuzz-codegen.md` when that read-only file exists.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`
- `qa/changes/<change-id>/review/fuzz-plan-review.json`
- `qa/changes/<change-id>/review/fuzz-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`

Bounded style inputs when present:

- existing `tests/fuzz/**`
- existing `tests/testdata/**`
- `tests/config.py`

Do not re-evaluate gate verdicts from review prose. Trust the graph precondition that scheduled this skill.

## Outputs

Write:

- generated fuzz tests under `tests/fuzz/` per the plan Target Files
- shared builders under `tests/testdata/` only when marked create-if-missing
- `qa/changes/<change-id>/codegen/fuzz-codegen-summary.md`

Test function names MUST be `test_<case_id_lowercase>__<description>`.

## Boundaries

Do not write plan files, review JSON, or checks JSON.

Do not invent auth tokens or import API/E2E adapters.

Do not run pytest.
