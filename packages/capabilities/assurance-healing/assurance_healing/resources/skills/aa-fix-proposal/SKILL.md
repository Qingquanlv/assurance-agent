# Fix proposal

Capability-owned fix-proposal skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn authenticated execution evidence and a closed generated mapping into a typed
fix proposal. Schema truth is `assurance_healing.contracts` for `FixProposalResultV1`.

## Inputs

### required

- locked change identity
- closed generated mapping paths
- allowed test roots
- baseline, candidate, and policy digests
- capability leaf catalog

### optional

- prior proposal digest
- approval requirement flag

## Outputs

### required

- structured `FixProposalResultV1`
- eligible items name exact files already present in the closed mapping
- ineligible items stay out of the apply set

## Rules

- Consume only the locked mapping, allowed roots, and declared capability leaves.
- Do not invent files, capabilities, or product-code edits.
- Do not write product trees (`app/`, `src/`, `web/src/`).
- Do not select a host adapter or remember prior conversation state.
- When a proposal needs review, set `needs_review: true` and leave apply to an
  approved receipt.
- Write the typed result to `qa/results/healing/fix-proposal.json`.
- Return the typed result and stop.

## File byte contract

The proposal reference authenticates canonical JSON bytes. Include all fields
and defaults from the result schema, including empty arrays. Keep each
`files_to_modify` array sorted and duplicate-free. Serialize the complete result
as UTF-8 with sorted object keys, compact separators, literal Unicode, and
exactly one trailing newline. Use the standard serializer, not handwritten JSON:

```python
data = (json.dumps(result, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
output_path.write_bytes(data)
```

Reopen that exact allowed output and return the same JSON object.
