# assurance-product

Independent Assurance composition root for Graph Engine. `aa` is owned by
`assurance-product`. The wheel exposes one `graph_engine.products` entry
point:

- `assurance-opencode` → `AssuranceOpenCodeProductProvider`

The provider ships one exact product declaration and the six Phase 4
capability coordinates plus `agent-runtime-opencode==0.1.0`.

The OpenCode adapter is an optional extra: `assurance-product[opencode]`.
The package does not scan a SUT, import deleted distributions, select a
deployment or config provider, or embed runtime binding values.

YAML replaces graph and contract text. Python wheels add installed capability.
Project `.aa/` holds organization configuration only. The engine does not load
executable plugins from the system under test.

Installed commands are `aa compile`, `aa start`, `aa run`, `aa status`,
`aa resume`, `aa export`, `aa archive`, `aa bindings build`, and
`aa lock show`. Delivery is `aa run` to achieved, then `aa export`, then
optional `aa archive`.

## User API+DB and API+DB+Trace benchmarks

Stage-1 item `opencode-user-api-db` selects `api_db.v1`. Stage-2 item
`opencode-user-api-db-trace` selects only `api_db_trace.v1`. Both use the
same User requirement, DB observer/comparator, and business assertion IDs.
Do not copy a second expected set.

Live Agent full (optional operator tool, not ordinary CI):

```bash
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-business-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault drop-write-span
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault early-completed
uv run python benchmark/assurance-product/run_item.py --item opencode-user-api-db-trace --adapter opencode --fault refactor
```

Ordinary CI is uv-workspace lint/type/import/pytest plus the three wheel
smoke scripts. It does not build images, start Docker/Colima, or read
qualification files. Stage-2 three-class delivery is recorded in
`benchmark/assurance-product/stage2-delivery.md`.
