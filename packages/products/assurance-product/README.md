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

`aa operator start|status|stop|resume|assessment` is the product facade used by
a QA panel. It allocates one Change, writes the effective spec, and drives the
existing bootstrap. It does not choose graph nodes or seal evidence.

Operator-started runs record `ownership: shared` and a loopback
`requested_opencode_endpoint`. That handle is a borrowed server: stop, resume,
and process exit do not signal it. Standalone `aa bootstrap` still starts a
private OpenCode process and stops that process only. A lost shared server
becomes an explicit terminal failure (exit 30), not an empty success.

Each run creates one unprompted root session on that server and stores
`root_session_id`. Attempt sessions are children of that exact parent and the
same run worktree. `origin_session_id` correlates the operator chat and is not
the parent. A session that already has a parent cannot start another run.
Bounded leaf agents have the `assurance` tool disabled.

`aa operator assessment` reads one explicit
`change → plan digest → coverage epoch → batch` chain. The assessment is
returned only when the committed plan bytes, issue-evidence manifest entry,
and assessment bytes agree. Otherwise the result is `assessment: null` with
`plan_mismatch`, `missing_ref`, `assessment_changed`, or `not_assessed`.
Manual pytest output is not that chain. Remote OpenCode endpoints are rejected;
this operator accepts loopback only.
