---
name: aa-performance-codegen
description: Generate performance tests from a reviewed performance plan after the graph gate has passed. Reads exact plan, review, checks, cases, config, and L1 inputs. Does not execute pytest.
---

## Purpose

Translate a reviewed performance plan into executable load tests. This skill does **not** execute pytest and does **not** decide workflow progression — the graph gate is the progression authority.

Apply non-deprecated guidance from `.aa/memory/aa-performance-codegen.md` when that read-only file exists.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/review/performance-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`

Bounded style inputs when present:

- existing `tests/perf/**`
- existing `tests/testdata/**`
- `tests/config.py`

Do not re-evaluate gate verdicts from review prose. Trust the graph precondition that scheduled this skill.

## Outputs

Write:

- generated performance tests under `tests/perf/` per the plan Target Files
- shared builders under `tests/testdata/` only when marked create-if-missing
- `qa/changes/<change-id>/codegen/performance-codegen-summary.md`

Test function names MUST be `test_<case_id_lowercase>__<description>`.

## Boundaries

Do not write plan files, review JSON, or checks JSON.

Do not invent thresholds or import unrelated layer adapters.

Do not run pytest.
