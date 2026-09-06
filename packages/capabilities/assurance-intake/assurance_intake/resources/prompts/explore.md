# Explore prompt

Produce explore artifacts from requirement text and shallow product-structure evidence.

The input names the caller's candidate test families and the authenticated policy,
capability-catalog, and data-knowledge sources. Treat candidates as the resolver's
allowed starting set, not as a preselected answer. Emit one recommendation for every
family so the deterministic resolver can freeze the initial plan after Explore.

Write the complete `explore/exploration.json`. Do not write `case.yaml`. Do not modify
explore context owned by the deterministic context step.
Do not use glob to check either Explore path (`context.json` or `exploration.json`).
Ignored change-local files can be absent from repository search while exact native
reads still work; read the prepared context and written output by their exact paths.

Record source-structure evidence with medium confidence. Autonomous, degraded, and
no-source runs must still write a complete valid `exploration.json`; use empty
evidence-backed signal arrays and explicit evidence limitations instead of omitting the
artifact. Missing or unreadable deterministic context is a failure and must not be
reported as structured success with an empty output list.

Resolve open questions according to the locked interaction mode. Do not invent product behavior.
Determine required business obligations independently of family recommendations. In
particular, a declined E2E recommendation does not make a required user journey
inapplicable or remove it from minimum required coverage.
