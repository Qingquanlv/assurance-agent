# Dashboard projection

Capability-owned dashboard skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

This skill describes the quality dashboard *projection*. It does not launch a
browser, start a server, or implement frontend UI. The deterministic dashboard
handler emits a closed summary from authenticated report and inspect artifacts.

## Inputs

### required

- locked quality report
- locked quality-gate and metrics digests

## Outputs

### required

- structured dashboard projection: change identity, `final_status`, `quality_score`,
  `issue_risk`, and source digests

## Rules

- Read only the locked report and inspect projections.
- Do not write case files or an orchestration state file.
- Do not include provider session transcripts or secret-bearing diagnostics.
- Do not start a local server or open a browser.
- Use the locked execution binding when this skill is prepared.
- Return the typed projection and stop.
