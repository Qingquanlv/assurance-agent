# Static Feature Entrypoints for All Six Capabilities

## Intent

Each capability wheel owns one public Python composition interface in `feature.py`.
The Product explicitly installs six such interfaces instead of reaching into
their `contracts`, `operations`, and `graphs` packages separately. This closes
the gap between the previously discussed layout and the implemented lifecycle
refactor. It does not change graph behavior.

## Interface

`FeatureSpec` is an immutable, typed descriptor in the graph-engine boot layer.
It carries the existing plugin class, Agent and deterministic Task contract
catalogs, output-route templates, graph-factory reference, and Agent Task
classes. Each `assurance_*/feature.py` exports one `FEATURE` value assembled
from that wheel's existing definitions and owns its public graph-bundle type;
`graphs/factory.py` imports that type without moving topology. Empty Agent fields are valid for
execution, which has only deterministic Tasks.

The Product has one explicit tuple of the six `FEATURE` values. Its graph
factory allowlist, Agent and Task catalogs, output routes, and runtime Task
binding use this tuple. No discovery, decorator registry, SUT loading, or
second graph DSL is introduced. The pinned wheel source catalog and plugin
declarations remain independent security allowlists; the Product checks that
they agree with the six Feature identities.

## Stable Behavior

- `graphs/factory.py` continues to own StateGraph topology and the 28 bundle
  exports. `feature.py` points to the factory; it does not compile a graph.
- Existing 26 Agent and 19 deterministic Task contracts, 48 Attempt node
  registrations, 28 bundle exports, and 15 Product roots retain their
  identifiers and semantics.
- Existing `@before`, `run`, `@after`, optional `@finally_`, injected OpenCode
  phase, retry policy, Kernel sealing, and host authorization are unchanged.
- `.aa/` remains organization configuration, never a source of executable
  Feature definitions.

## Validation

Fail closed on duplicate Feature owners, duplicate contract IDs, Agent Task
classes without matching contracts, duplicate Task classes, and owner/plugin
identity drift. Tests exercise the six-entry Product assembly and all existing
graph/entrypoint integration tests. CI lint/type checks, full pytest, and wheel
smokes validate the installed distribution path.
