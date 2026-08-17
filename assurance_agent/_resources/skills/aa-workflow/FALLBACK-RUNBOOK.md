# AA Workflow — Operator Runbook (schema v2)

> GraphRuntime is the **only** progression writer. There is no hand-edited phase
> fallback and no Scheme E agent orchestration loop.

## Command surface

```bash
aa workflow run --change <id> --entrypoint full|intake|execute|case [--params '{...}']
aa workflow resume --change <id>                                 # plain resume / lease abandon
aa workflow resume --change <id> --interrupt <id> --action <a> --reason <text> [--who <who>]
aa workflow import-checkpoint --change <id> --manifest <path>
aa status --change <id> [--next] [--json]                        # GraphStatus projection
aa decide --change <id> ...                                      # non-graph policy only (e.g. allow_test_changes)
```

OpenCode `workflow_start` accepts `entrypoint: full | intake | execute | case` (not `scope`).

## Operator may

1. Inspect `aa status --json` (pending tasks, pending interrupts, next retry).
2. Wait until `next_retry_at` and plain-resume an expired attempt (`aa workflow resume`).
3. Resolve a listed interrupt with `--interrupt` / `--action` / `--reason`.
4. Run a **validated** checkpoint import (`aa workflow import-checkpoint`) for fixture/benchmark mid-graph entry.

## Operator must never

- Delete or rewrite `events.jsonl`
- Reset budgets by hand
- Edit checkpoint JSON under `.graph-runtime/`
- Mark a task complete from bare files (artifact presence ≠ completion)
- Run removed `aa state apply` / `aa state heal`
- Use `--scope` (removed; use `--entrypoint`)
- Use graph-gate `aa decide` (human safety approval is interrupt resume)

## Recovery procedures

| Condition | Action |
|---|---|
| Live lease (holder still alive) | Do not steal; wait or stop the holder process |
| Expired lease | `aa workflow resume` — runtime abandons and retries |
| Graph digest drift | Fail closed; re-pin schema/contracts or re-import |
| Artifact hash drift | Fail closed; restore audited artifacts or re-run the node |
| Workspace drift | Fail closed; discard private workspace and resume |
| Retry exhaustion | Terminal stop; inspect ledger; do not hand-complete |
| Corrupted checkpoint cache | Delete checkpoint cache only; ledger rebuilds projection |

## Policy vs graph

- `allow_test_changes` remains a one-use `aa decide` **policy** path (not graph progression).
- Archive status is committed by the **archive graph node**, not `aa state apply --phase archive`.
- Healing status is a **graph terminal operation**, not `aa state heal`.
- Human safety approval uses **interrupt resume**.

## Eval / fixtures

Mid-graph eval enters only through a hash-complete `.graph-runtime/import-manifest.yaml`
produced by fixture seeding. Bare `workflow-state.json` phase markers are never authority.
