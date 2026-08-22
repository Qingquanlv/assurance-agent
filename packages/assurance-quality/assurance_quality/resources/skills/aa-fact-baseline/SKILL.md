# Fact baseline

Capability-owned fact-baseline skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Collect only stable product or environment facts that planners must not invent.
Schema truth is `assurance_quality.contracts` for `FactBaselineResultV1`.

## Inputs

### required

- locked change identity
- owned source evidence identifiers
- seed or init sources named in the canonical business input

### optional

- read-only database probe results already present in the locked evidence

## Outputs

### required

- structured `FactBaselineResultV1`
- `source` is `seed_file`, `db_probe`, `both`, or `unavailable`
- `source_evidence_ids` lists only owned evidence

## Rules

- Never invent credentials, role ids, route prefixes, token headers, or database facts.
- Never emit `facts.endpoints` or `facts.*_endpoints`. A single `login_endpoint` is allowed.
- This is not a performance baseline.
- If a fact is uncertain, omit it or set it to null and add a warning.
- Do not mutate product data, seeds, fixtures, or cases.
- Use the locked execution binding from the prepare request.
- Return the typed result and stop.
