---
name: aa-fact-baseline
description: "Use when AA workflow needs Phase 2.4 fact collection after case review passes and before planning/review. Applies to auth, built-in roles, route prefix, token header, seed data, or optional read-only DB probe facts. Not for performance threshold baselines."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.case_review.status == pass` (case review gate must be cleared).
3. Read input files from disk:
   - `.aa/config.yaml` (optional; needed for `db_probe` setting and project conventions)
   - Seed/init sources when present, for example `app/core/init_app.py`, `db/seed*.py`, `fixtures/**`, or framework-equivalent bootstrap files
   - `qa/changes/<change-id>/cases/**/case.yaml` and `proposal.md` only as context for which facts matter
4. If `.aa/config.yaml` enables `db_probe: true`, run only documented read-only probes. Never mutate the database.
5. Use files/probe output as the sole source of truth. Do not guess unknown facts.

**After completing work:**

1. Write `qa/changes/<change-id>/facts/fact-baseline.json`.
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.fact_baseline.status = done | unavailable | failed`
   - `phases.fact_baseline.source = seed_file | db_probe | both | unavailable`
   - `phases.fact_baseline.file = facts/fact-baseline.json`
   - `phases.fact_baseline.warnings = [...]`

---

# AA Fact Baseline

## Purpose

Collect stable product/environment facts that downstream planners and reviewers must not invent: built-in roles, default/auth test credentials, route prefix, token header/scheme, and seed data that affects test setup.

This is **not** a performance baseline. Performance pass/fail targets come from Performance cases and `aa-performance-plan` absolute thresholds.

## Output Contract

Write exactly one primary artifact:

```text
qa/changes/<change-id>/facts/fact-baseline.json
```

Use this shape:

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "generated_at": "<ISO timestamp>",
  "source": "seed_file | db_probe | both | unavailable",
  "seed_file": "<path or null>",
  "facts": {
    "builtin_roles": [
      {"id": 1, "name": "管理员", "source": "seed_file"}
    ],
    "auth": {
      "default_username": "admin",
      "default_password": "<from seed or null>",
      "source": "seed_file | db_probe | unavailable"
    },
    "route_prefix": "/api/v1",
    "token_header": "Authorization",
    "token_scheme": "Bearer"
  },
  "warnings": []
}
```

For `source = seed_file | db_probe | both`, `schema_version` is required and must be `"1.0"`.

If no seed/init source is readable and no read-only DB probe is configured, still write `fact-baseline.json` with:

```json
{
  "source": "unavailable",
  "facts": {},
  "warnings": ["seed file not found and db_probe disabled"]
}
```

This is not a hard stop. Planners must carry the warning and reviewers must mark fact validation as skipped.

## Hard Rules

- Never create or modify product data, seed files, fixtures, or cases.
- Never write `workflow-state.yaml` when running as a dispatched subagent.
- Never invent credentials, role ids, route prefixes, token headers, or DB facts.
- Keep complete route inventories out of this artifact: never emit `facts.endpoints` or
  `facts.*_endpoints`. A single authentication endpoint such as `login_endpoint` is
  allowed; Explore and each Plan independently verify the complete route surface.
- Never treat this as a performance baseline or historical regression target.
- If a fact is uncertain, omit it or set it to `null` and add a warning.

## Final Response

Report:

- `facts/fact-baseline.json` path
- `source`
- key facts found
- warnings
- state delta values for `phases.fact_baseline`
