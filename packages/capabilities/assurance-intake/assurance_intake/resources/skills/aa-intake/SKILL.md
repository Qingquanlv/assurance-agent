# Intake

Capability-owned intake bootstrap skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

This node only creates the change workspace. Explore, case-design, and case-review
are later graph nodes. Do not sequence them here.

## Inputs

The JSON instruction part already contains the locked `change_id` and `requirement`.
Treat those values as authoritative. Do not ask for scope, change ID, or requirement.

The host prepare node has already written `qa/requirement.md` from the accepted
input. Read that file. Do not rewrite, expand, or replace it.

## Write the change directory

`qa/` is allowed to be missing. Create it by writing files.
Do not require the directory to exist first. Do not tell the user to initialize
the change. Do not look up workflow status for a missing directory.

Write this file with the native write tool (creating parent directories is part
of the write):

1. `qa/.qa.yaml` — `change_id` only; do not invent approval

Call the native `write` tool exactly once for that authorized file. Then read
`qa/requirement.md` and `qa/.qa.yaml` back and verify their content. A final JSON
response without those successful tool calls is invalid, even if the paths are
listed correctly.

## Forbidden

- Do not ask clarifying or scope questions.
- Do not wait for a human choice before writing.
- Do not require `qa/` to exist first.
- Do not run explore, case-design, or case-review in this node.
- Do not edit tests or product code.
- Do not write a runtime ledger.
- Do not write, rewrite, or expand `qa/requirement.md`.

## Completion

After the writes succeed, return structured JSON only:

```json
{"output_files":["qa/.qa.yaml"]}
```

Stop. Do not continue into execute scope.
