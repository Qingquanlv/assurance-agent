# Fact baseline

Capability-owned fact-baseline skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Collect only stable product or environment facts that planners must not invent.
Schema truth is `assurance_quality.contracts` for `FactBaselineResultV1`.

## Inputs

### required

- locked change and coverage epoch
- authenticated Reviewed Case and its plan, preparation, case, and review refs
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
- When administrator authentication is required, inspect the startup initialization or seed source
  named by the locked input. Record `admin_username` and `admin_password` in `facts` only when that
  source deterministically resolves both values used to create the initial administrator.
- If either initial administrator value is indirect, runtime-dependent, contradictory, or
  unavailable, omit both credential facts and add a warning that identifies the unresolved source.
- Never read `.env` or `*.env` files to obtain credentials. Source-proven initialization literals
  and deterministic constants are allowed; environment values and existing database contents are not.
- Never emit `facts.endpoints` or `facts.*_endpoints`. A single `login_endpoint` is allowed.
- This is not a performance baseline.
- If a fact is uncertain, omit it or set it to null and add a warning.
- Do not mutate product data, seeds, fixtures, or cases.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/facts/fact-baseline.json`.
- Return the typed result and stop.
