# Intake

Capability-owned intake bootstrap skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

This node only creates the change workspace. Explore, case-design, and case-review
are later graph nodes. Do not sequence them here.

## Inputs

The JSON instruction part already contains the locked `change_id` and `requirement`.
Treat those values as authoritative. Do not ask for scope, change ID, or requirement.

## Write the change directory

`qa/changes/<change-id>` is allowed to be missing. Create it by writing files.
Do not require the directory to exist first. Do not tell the user to initialize
the change. Do not look up workflow status for a missing directory.

Write these files with the native write tool (creating parent directories is part
of the write):

1. `qa/changes/<change-id>/requirement.md` — the locked requirement text
2. `qa/changes/<change-id>/.qa.yaml` — `change_id` only; do not invent approval

## Forbidden

- Do not ask clarifying or scope questions.
- Do not wait for a human choice before writing.
- Do not require `qa/changes/<change-id>` to exist first.
- Do not run explore, case-design, or case-review in this node.
- Do not edit tests or product code.
- Do not write a runtime ledger.

## Completion

After the writes succeed, return structured JSON only:

```json
{"output_files":["qa/changes/<change-id>/requirement.md","qa/changes/<change-id>/.qa.yaml"]}
```

Stop. Do not continue into execute scope.
