### Task 14: Dark-Ship Exact Persona Mapping and Prove Attempt Workspace Requests

**Files:**
- Create: `assurance_agent/workflow/graph/assurance_personas.py`
- Modify: `assurance_agent/workflow/driver/opencode_adapter.py`
- Modify: `tests/unit/driver/test_opencode_adapter.py`

**Interfaces:**
- Produces: exact dormant `ASSURANCE_PERSONA_BY_TARGET`, `expected_assurance_persona(target)`, and request-directory continuity tests.
- Consumes: the sixteen exact skill targets, `AgentRequest.workspace_root`, packaged persona documents, and mocked OpenCode create/prompt/status transport.
- Preserves: current handler routing and packaged persona permissions. Task 15 switches the handler to the registry and tightens persona documents in the atomic activation; no external OpenCode dependency enters CI.

- [ ] **Step 1: Add the exact target/persona table tests**

  Require planners plus API/E2E plan fixers to `aa-doc-author`, four reviewers to `aa-reviewer`, and four codegen plus API/E2E codegen fixers to `aa-test-author`. Reject a missing target, extra assurance-like target, and schema persona mismatch. Do not change handler fallback in this task.
- [ ] **Step 2: Add request-directory continuity tests**

  Mock transport and assert session creation, prompt dispatch, every status poll, and reconnect use exactly `AgentRequest.workspace_root`. Reject a response/status request made with host root, change root, or a prior attempt's directory.
- [ ] **Step 3: Run tests and observe any request-directory gaps**

  ```bash
  uv run pytest -q \
    tests/unit/driver/test_opencode_adapter.py
  ```

  Expected: the new mapping tests fail until the registry exists; directory tests pass only if create, prompt, status, and reconnect all use the attempt root.
- [ ] **Step 4: Implement the dormant mapping and request validation**

  Define the exact registry without importing it from `AgentHandler` yet. Preserve current OpenCode calls while making directory propagation explicit and tested at every request boundary.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/driver/test_opencode_adapter.py
  uv run ruff check assurance_agent/workflow/graph/assurance_personas.py assurance_agent/workflow/driver/opencode_adapter.py tests/unit/driver/test_opencode_adapter.py
  uv run pyright
  ```

  Expected: exact persona and directory tests pass for all sixteen targets.
- [ ] **Step 6: Commit dormant persona mapping and adapter coverage**

  ```bash
  git add assurance_agent/workflow/graph/assurance_personas.py \
    assurance_agent/workflow/driver/opencode_adapter.py \
    tests/unit/driver/test_opencode_adapter.py
  git commit -m "feat(agent): define assurance personas and pin attempt directories"
  ```

