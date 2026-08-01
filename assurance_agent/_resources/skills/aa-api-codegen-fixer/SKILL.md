---
name: aa-api-codegen-fixer
description: Apply authorized API codegen fixes and emit a path-specific apply intent.
---

## Purpose

Apply authorized API test fixes using the graph-owned fixer-authority projection and emit only `healing/api-apply-intent.json` plus authorized private/shared test writes. Do not invoke `aa heal record-apply`.

## Inputs

### required

- `change:healing/fix-proposal.json`
- `change:healing/fixer-authority.json`
- `change:codegen/api-codegen-summary.md`
- `change:codegen/api-generated-files.json`
- `change:execution/**`
- `change:inspect/**`
- `repo:tests/api/**`

### optional

- `change:healing/fixer-proposal-approval.json`
- `repo:tests/testdata/**`

## Outputs

### required

- `change:healing/api-apply-intent.json`

### conditional

- `repo:tests/api/**`
- `repo:tests/testdata/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the target intent and authorized private/shared test changes.

Do not write apply-summary JSON, safety fragments, or call host healing CLI commands.
