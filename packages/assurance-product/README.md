# assurance-product

Independent Assurance composition root for Graph Engine. The wheel exposes
exactly two `graph_engine.products` entry points:

- `assurance-opencode` → `AssuranceOpenCodeProductProvider`
- `assurance-cursor` → `AssuranceCursorProductProvider`

Both providers share one exact product declaration and the same six Phase 4
capability coordinates. Their source catalogs differ only by the selected
runtime adapter (`agent-runtime-opencode==0.1.0` or
`agent-runtime-cursor==0.1.0`).

Adapters are optional extras: `assurance-product[opencode]` and
`assurance-product[cursor]`. The package does not scan a SUT, import deleted
distributions, select a deployment or config provider, or embed runtime binding
values.
