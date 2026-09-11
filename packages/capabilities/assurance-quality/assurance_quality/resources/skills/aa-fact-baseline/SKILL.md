# Fact baseline

Capability-owned fact-baseline skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Collect only stable product or environment facts that planners must not invent.
Schema truth is `assurance_quality.contracts` for `FactBaselineResultV1`.

## Inputs

### required

- locked change, coverage epoch, and execution batch identity
- authenticated Reviewed Case, mapping, execution, trace, gap, metrics, and sufficiency refs
- seed or init sources named in that canonical business input

### optional

- read-only database probe results already present in the locked evidence

## Outputs

### required

- structured `FactBaselineResultV1`
- `change_id` echoes the locked change exactly
- `source` is `seed_file`, `db_probe`, `both`, or `unavailable`
- `source_evidence_ids` lists only evidence present in the locked input

## Rules

- Never invent credentials, role ids, route prefixes, token headers, or database facts.
- Never emit `facts.endpoints` or `facts.*_endpoints`. A single `login_endpoint` is allowed.
- This is not a performance baseline.
- If a fact is uncertain, omit it or set it to null and add a warning.
- Do not mutate product data, seeds, fixtures, or cases.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/facts/fact-baseline.json`.
- Return the typed result and stop.
