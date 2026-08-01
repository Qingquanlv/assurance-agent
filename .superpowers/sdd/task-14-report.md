# Task 14 Report — Dark-Ship Exact Persona Mapping and Prove Attempt Workspace Requests

## Status

**DONE**

## What Changed

### Dormant exact persona registry

Created `assurance_agent/workflow/graph/assurance_personas.py` with:

- Frozen `ASSURANCE_PERSONA_BY_TARGET` (exactly sixteen skill targets)
- `expected_assurance_persona(target)` accepting bare skill ids or `skill:`-prefixed targets; unknown targets raise `KeyError`

Exact table:

| Persona | Targets |
|---|---|
| `aa-doc-author` | four planners + `aa-api-plan-fixer` + `aa-e2e-plan-fixer` |
| `aa-reviewer` | four plan reviewers |
| `aa-test-author` | four codegen + `aa-api-codegen-fixer` + `aa-e2e-codegen-fixer` |

`AgentHandler.agent_for_skill` and packaged persona permission floors were **not** changed (Task 15 activation).

### Attempt-directory continuity

`OpenCodeAdapter._request` binds `?directory=` explicitly: when invoke supplies an attempt directory it never falls back to the host/SUT root; empty directory fails closed. `invoke` documents and keeps create / prompt / every status poll / reconnect on `AgentRequest.workspace_root` only.

### Tests (`tests/unit/driver/test_opencode_adapter.py`)

- Exact sixteen-target table + rejection of missing and extra assurance-like targets
- Packaged schema `agent:` fields match the registry for all sixteen skills
- Packaged persona documents cover registry persona names
- Mock-transport continuity: create, prompt, status polls, and reconnect use the attempt workspace; host root, change root, and prior-attempt directories are rejected by assertion

No external OpenCode server is required.

## Verification

```text
uv run pytest -q tests/unit/driver/test_opencode_adapter.py
→ 34 passed, 1 warning

uv run ruff check assurance_agent/workflow/graph/assurance_personas.py \
  assurance_agent/workflow/driver/opencode_adapter.py \
  tests/unit/driver/test_opencode_adapter.py
→ All checks passed

uv run ruff format --check assurance_agent/workflow/graph/assurance_personas.py \
  assurance_agent/workflow/driver/opencode_adapter.py \
  tests/unit/driver/test_opencode_adapter.py
→ 3 files already formatted

uv run pyright assurance_agent/workflow/graph/assurance_personas.py \
  assurance_agent/workflow/driver/opencode_adapter.py \
  tests/unit/driver/test_opencode_adapter.py
→ 0 errors, 0 warnings, 0 informations
```

Full-repo `uv run pyright` currently reports 1 pre-existing error in
`assurance_agent/workflow/graph/resume_compatibility.py` (Task 12 WIP; out of
Task 14 scope). Task 14 files are clean.

## Files Changed (edit only; no git add/commit)

```
assurance_agent/workflow/graph/assurance_personas.py          (new)
assurance_agent/workflow/driver/opencode_adapter.py
tests/unit/driver/test_opencode_adapter.py
.superpowers/sdd/task-14-report.md
```

Left untouched per instructions: `cursor-loop` helpers/tests, handler fallback,
packaged persona permissions, and Task 12 surfaces (`graph_events`, models,
checkpoint, replay_binding, runtime, driver_state).

Suggested commit message (controller):

```
feat(agent): define assurance personas and pin attempt directories
```

## Deferred / Out of Scope

- Task 15: switch `AgentHandler` to `ASSURANCE_PERSONA_BY_TARGET` and tighten packaged persona edit floors
- Real OpenCode server sandbox verification (CI uses mock transport only)
