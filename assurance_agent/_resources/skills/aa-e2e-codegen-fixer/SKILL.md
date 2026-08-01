---
name: aa-e2e-codegen-fixer
description: Apply authorized E2E codegen fixes and emit a path-specific apply intent.
---

## Purpose

Apply authorized E2E test fixes using the graph-owned fixer-authority projection and emit only `healing/e2e-apply-intent.json` plus authorized private/shared test writes. Do not invoke `aa heal record-apply`.

## Inputs

### required

- `change:healing/fix-proposal.json`
- `change:healing/fixer-authority.json`
- `change:codegen/e2e-codegen-summary.md`
- `change:codegen/e2e-generated-files.json`
- `change:execution/**`
- `change:inspect/**`
- `repo:tests/e2e/**`

### optional

- `change:healing/fixer-proposal-approval.json`
- `repo:tests/testdata/**`

## Outputs

### required

- `change:healing/e2e-apply-intent.json`

### conditional

- `repo:tests/e2e/**`
- `repo:tests/testdata/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the target intent and authorized private/shared test changes. Do not write apply-summary JSON or call host healing CLI commands.
