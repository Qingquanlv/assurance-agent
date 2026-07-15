---
name: aa-fuzz-plan
description: "AA M3 Fuzz Stage 1: generate reviewable fuzz test plans from Fuzz cases before any code generation. Use when a Case Delta contains type:Fuzz cases. Reads case.yaml, selects type==Fuzz cases, and writes fuzz-plan.md, fuzz-codegen-plan.md, fuzz-review-summary.md. Uses schemathesis (OpenAPI schema-based robustness). Never generates test code, never runs pytest."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-fuzz-plan.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared domain builders live in `tests/testdata/domain/`; fuzz execution adapters live in `tests/fuzz/adapters/`; generated-input strategies live in `tests/fuzz/strategies/`.
- Strategies generate values only. Stateful setup/cleanup belongs to the fuzz adapter and maps through `capabilities.domain_factories` plus `capabilities.adapters.fuzz`.
- Do not reuse API/E2E pytest fixtures as fuzz adapters. Shared files are `create-if-missing` for the first active layer and reuse-only thereafter.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.case_review.status == pass` (case design + review gate cleared).
3. Read input files from disk:
   - `qa/changes/<change-id>/cases/**/case.yaml`
   - `qa/changes/<change-id>/proposal.md`
   - `.aa/config.yaml`
4. From `added` and `modified` only, select cases with `type == Fuzz` and `automation.required == true`. Skip `removed`. If none, stop and report (nothing to plan).
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/plans/fuzz-plan.md`
   - `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
   - `qa/changes/<change-id>/plans/fuzz-review-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.fuzz_plan.status = done`
   - `phases.fuzz_plan.selected_cases` = selected Fuzz case_ids

---

# AA Fuzz Plan

## Purpose

Turn `type: Fuzz` cases into a reviewable robustness-test plan. Fuzz testing exercises an endpoint with schema-derived and boundary inputs to verify the endpoint does not crash (no 5xx) and does not wrongly reject schema-valid input. It is **input-robustness only** — it never replaces functional assertions.

This skill does **not** generate test code and does **not** run pytest. It is Stage 1 of the Fuzz flow; `aa-fuzz-codegen` runs only after `aa-fuzz-plan-reviewer` clears the gate.

## Selection Rule

- Only process cases under `added` / `modified` with `type == Fuzz` and `automation.required == true`.
- Every selected Fuzz case MUST have `automation.fuzz.endpoints` non-empty and `related_cases` pointing at a functional (API) case. If not, record a blocker in `fuzz-review-summary.md` (the reviewer will hard-block).

## Inputs

```text
qa/changes/<change-id>/cases/**/case.yaml      (type == Fuzz)
qa/changes/<change-id>/proposal.md
.aa/config.yaml                               (fuzz settings, if present)
.aa/data-knowledge.yaml                       (domain factory + fuzz adapter capabilities)
```

Recommended (for schema source resolution):

```text
the project's OpenAPI schema source (e.g. ASGI app exposing /openapi.json)
tests/fuzz/**                                  (existing fuzz tests, for style)
```

## Outputs

### `plans/fuzz-plan.md`

For each Fuzz target, document:

- **Target endpoint(s)** — from `automation.fuzz.endpoints`
- **Schema source** — how schemathesis obtains the OpenAPI schema (prefer `from_asgi` in-process; fall back to `from_uri` against a live service when in-process is not feasible)
- **Expectations** — from `automation.fuzz.expectations` (default robustness checks: no 5xx; schema-valid input not rejected with 400; response conforms to declared schema)
- **Related functional case** — the API `case_id` this Fuzz case hardens (`related_cases`)
- **Out of scope** — explicit non-goals (Fuzz does not assert business outcomes)

### `plans/fuzz-codegen-plan.md`

- **Target Files** — `tests/fuzz/test_<module>_fuzz.py`; `tests/fuzz/strategies/<module>.py` for reusable value generation; `tests/fuzz/adapters/<module>.py` only for stateful setup/cleanup; shared `tests/testdata/domain/<entity>.py` is marked `create-if-missing` or `reuse`
- **Schema acquisition strategy** — `from_asgi` vs `from_uri`, app import path or base URL
- **Auth strategy** — how authenticated endpoints receive a token (reuse existing fixtures/data-knowledge; never hardcode real tokens)
- **Capability Mapping** — shared domain capability, fuzz adapter transport, cleanup, and strategy module; no API/E2E adapter reuse
- **Generated File Policy** — append vs overwrite; do not clobber existing fuzz tests

### `plans/fuzz-review-summary.md`

- **Plan Readiness** — `ready | not_ready`
- **Codegen Readiness** — `ready | ready_with_warnings | not_ready`
- **Blockers** — e.g. missing schema source, Fuzz case without related API case, endpoint needs auth but no strategy
- **Needs Review** — open questions

## Hard Rules

- Fuzz is **additive**: it never replaces the functional API/E2E coverage of an endpoint.
- Never write method/path/schema details back into `case.yaml`.
- Never generate test code or run pytest in this skill.
- A Fuzz case without a `related_cases` link to a functional case is a blocker — do not mark Plan Readiness `ready`.
- Do not invent schema fields; the schema comes from the project's OpenAPI source.
- Hand off to `aa-fuzz-plan-reviewer`; do not invoke `aa-fuzz-codegen` directly.
