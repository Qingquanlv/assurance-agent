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
`aa resume`, `aa bindings build`, `aa lock show`, and `aa retro show`. Delivery is
`aa run` to achieved.
