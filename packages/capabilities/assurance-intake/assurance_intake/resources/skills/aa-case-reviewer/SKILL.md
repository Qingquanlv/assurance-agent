# Case review

Capability-owned case-review skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.
Do not claim the generation-owned reviewer persona.

## Prepared source observations

The JSON instruction includes `planning_facts`: bounded static observations with
file digests, exact symbol names/signature shapes, fixture names, and environment
variable names. Use the indexed paths for direct reads instead of rediscovering
them. `unknown` and `uninspected_paths` never prove absence; imports, plugins,
dynamic registrations and transitive environment dependencies may be unresolved.
Environment names expose no values and do not establish availability or necessity.
Check original source for behavior, auth semantics and oracle claims. Keep owner
requirements and frozen assertion intent distinct from observed implementation;
a source defect must not weaken the expected test behavior.

Independently verify source behavior and owner-defined expectations. Before
returning findings, trace each defect across cases, proposal and MRC, and supply
all affected locators in the same review using the existing repair contract.
A repair must be reviewed again; a target repair count never authorizes a pass.

## Context Contract

Do not rely on prior conversation context.

### Frozen Explore oracle hard gate

Before judging whether a case has an owner-defined oracle, read the authenticated
Explore advisory. When a resolved priority hint or open question declares
`assertion_intent: assert_ideal`, that ideal behavior is a frozen oracle even when
the owner requirement omits it and current source does not enforce it. The reviewer
must not remove its covering case and must not mark its MRC row `skipped_by_scope`.
Treat a source mismatch as the product fault that downstream execution and issue
analysis are expected to expose.

**Before doing any work:**

1. Treat invocation of this skill by the product graph as authenticated predecessor proof
   that case-design finalize completed successfully. Graph phase state is not a workspace
   artifact: do not search `.qa.yaml` or any project file for `phases.case_design.status`.
   Its absence on disk is not a blocker.
2. If graph-owned run context explicitly includes `phases.case_design.status`, verify that
   it is `done`; an explicit contradictory value is a STOP condition.
3. Treat the JSON instruction's non-empty `review_input_paths` as the complete,
   authenticated case-design input set. Read every path exactly as provided with
   the native read tool. Do not discover these inputs with glob: ignored change
   files may be absent from search results. The set contains `.qa.yaml`,
   `requirement.md`, `proposal.md`, `trace/minimum-coverage-matrix.json`, and every exact
   `cases/<module>/case.yaml` path locked by `case_delta_paths`.
4. Independently read the relevant **product source code** for every product fact used in the verdict. At minimum inspect the implementation entry point plus the controller/service/schema/model or frontend component needed to verify the proposed scenarios. Do not treat proposal, case, Explore advisory, requirements, docs, or tests as product-fact evidence.
   A glob result of `No files found` is not evidence that product source is absent.
   Repository search may hide ignored product source even though exact reads and
   path-scoped grep can access it. If the proposal or a prior review names product
   source files, read those exact paths independently before attempting discovery.
   Otherwise seed discovery from exact paths in the requirement and use path-scoped
   grep under plausible source roots. Report source as unavailable only after exact
   reads and path-scoped grep fail; never infer absence from glob alone.
   For E2E cases, perform a path-scoped search for the exact component path and page or menu label under `app/` and `web/src/`,
   even when those roots are ignored. Read matching menu-registration or dynamic-route
   source before claiming the browser entry is unspecified. When that source proves
   an exact route or navigation binding, do not escalate the E2E entry mechanism to human review;
   route it as bounded `needs_fix` with the exact case precondition locator instead.
5. Distinguish author-owned outputs from external evidence. A missing or invalid
   case-design output (`.qa.yaml`, `proposal.md`, case YAML, or MRC matrix) is a
   mechanically fixable authoring defect: return `needs_fix` with an exact locator
   and repair plan. If relevant product source cannot be read, **STOP** before a
   normal pass/needs-fix verdict; do not invent product behavior. If the orchestrator
   requires a JSON artifact for that external-evidence failure, write
   `needs_human_review` with the missing evidence identified.
6. Treat the locked `requirement.md` as authoritative owner scope evidence. In
   particular, explicit numerical thresholds and load values in `requirement.md`
   are already owner-confirmed; do not require a duplicate value in graph approval
   metadata or product source. Requirements remain invalid as product-fact evidence.
7. Use files read in this invocation as the sole source of truth; do not rely on the case author's conclusions or prior conversation.
8. Exhaust the review in one pass: complete all review criteria before writing the verdict.
   Do not stop after the first defect. Include every currently observable finding
   and every bounded repair in the same review artifact so one defect does not
   consume one graph review round.
   For every added or modified case, cross-check every assertion against the
   locked owner requirement and independently read product source, then cross-check
   every trace key against that case's own steps and assertions. On review re-entry,
   re-review every case in full after checking the requested repairs; a repaired
   locator is not proof that the rest of the file is valid. Do not defer a currently
   observable finding to a later review round.

**After completing work:**

1. Write output files:
   - `qa/results/review/case-review.json`
   - `qa/results/review/case-review-summary.md`
2. Report the graph-owned state delta (the graph applies it; do not write an orchestration state file):
   - `phases.case_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.case_review.gate_file` = `review/case-review.json`

---

# AA Case Reviewer

## Purpose

Review AA QA case design artifacts and produce a structured review result.

Use this skill after `aa-case-design` has generated or updated QA case artifacts.

This skill is **read-mostly**. It should review the case proposal and case YAML files, then write review outputs under the change review directory.

It must not modify `proposal.md` or case YAML files.

---

## When to Use

Use this skill when:

- A new QA case delta has been generated.
- Existing case files were modified.
- You need to verify whether case artifacts are complete, testable, and ready for API/E2E planning.
- You need a machine-readable review gate before moving to planning.

---

## Inputs

The user or orchestrator should provide:

- `change_id`
- `case_delta_paths` — the exact current-change `cases/<module>/case.yaml` paths
- `review_input_paths` — the complete exact case-design input paths; read every item

Expected input files:

```text
qa/proposal.md
qa/requirement.md
qa/cases/<locked-module>/case.yaml
qa/.qa.yaml
```

Do not use glob to discover these files. Read the exact `review_input_paths` from
the JSON instruction even when `qa/**` is ignored by repository search.

Required independent evidence:

```text
<product source files relevant to the requirement>
```

Source verification is mandatory even when Explore or Case Design already read the same files. The reviewer must repeat the read independently and must not copy `source_code_evidence` from `explore/exploration.json` as a substitute.

Optional reference files:

```text
qa/cases/**/*.yaml
.aa/data-knowledge.yaml
docs/**/*.md
```

## Outputs

Write the following files:

```text
qa/results/review/case-review.json
qa/results/review/case-review-summary.md
```

Create the review directory if it does not exist.

---

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance past case review without the JSON file this skill writes.

- You **must** write `qa/results/review/case-review.json` as valid JSON with all required fields.
- You **must** write `qa/results/review/case-review-summary.md`.
- A natural language conclusion in chat is **not** a substitute for the JSON file. Never end with only a textual verdict.
- User approval in chat does not release the gate; only a valid `case-review.json` does. If the user says "approved", "looks good", or "continue", treat it only as review context — still validate every review criterion independently. Only write `decision == "pass"` when the artifacts satisfy all review criteria.
- If you cannot write the JSON file for any reason, treat the review as **failed** and report it — the workflow must treat a missing or invalid `case-review.json` as a STOP condition.
- `case-review.json` must contain a `source_verification` object with exactly these required fields: `independent: true`, a non-empty `reviewed_source_files` list of product source paths, and a non-empty `verified_claims` list. Every `verified_claims[]` item must contain `claim` and a non-empty `evidence_files` list drawn from `reviewed_source_files`. Paths under `qa/`, `.aa/`, `adapter-config/`, `docs/`, `requirements/`, and `tests/` do not count as product source.

---

## Review Scope

Review the generated case artifacts for:

- Requirement coverage
- Case clarity
- Case YAML schema consistency
- Testability
- API/E2E automation readiness
- Preconditions and test data
- Assertions
- Traceability
- Minimum Required Coverage (MRC) mapping from advisory to cases
- Risk and ambiguity
- Duplicate or conflicting cases
- Test layer assignments (API vs E2E per the decision tree in `aa-case-design`)

### Explore soft check (Phase 0.5 — warning only, not blocker)

When `explore/exploration.json` exists and `phases.explore.status == done`:

```
FOR EACH watchlist item WHERE confidence == high:
  proposal.md ## Explore Input MUST list WL-* as disposition in [adopted, override]
  IF override → reason required in table
ELSE emit warning: RISK-ADVISORY-WATCHLIST-UNADDRESSED
```

Finding example:

```json
{
  "id": "RISK-ADVISORY-WATCHLIST-UNADDRESSED",
  "severity": "low",
  "human_review_required": false,
  "message": "High-confidence watchlist item WL-001 not addressed in proposal.md Explore Input"
}
```

Also **warn** (non-blocker) if `case.yaml` contains advisory trace metadata (`evidence_ids`, `explore`, `risk_advisory` (legacy), `WL-*` / `PH-*` / `HS-*` (legacy) as dedicated trace fields).

### Test Types Considered check (warning only, not blocker)

`proposal.md` MUST contain a `## Test Types Considered` section listing **all four layers** (API / E2E / Fuzz / Performance), each marked `selected` or `declined` with a reason. This is the evidence that Fuzz/Performance were offered to the user during clarification (Category 3 hard rule in `aa-case-design`).

```text
IF proposal.md has no "## Test Types Considered" section
   OR any of the four layers is missing from it
   OR a declined layer has no reason
→ emit warning: TEST-TYPES-NOT-CONSIDERED
```

Finding example:

```json
{
  "id": "TEST-TYPES-NOT-CONSIDERED",
  "severity": "low",
  "human_review_required": false,
  "message": "proposal.md Test Types Considered is missing or does not cover all four layers (API/E2E/Fuzz/Performance) with selected/declined decisions"
}
```

### Minimum Required Coverage gate (hard)

When `risk-advisory/advisory.json` or `explore/exploration.json` contains `minimum_required_coverage`:

```text
FOR EACH required MRC item:
  It MUST appear as one exact row in trace/minimum-coverage-matrix.json
  A covered row MUST name at least one existing case in covered_by_cases
  The covering case type/layer MUST match the MRC layer (api/e2e/both)
  Closed-category keys (data_integrity / negative / auth / e2e_if_enabled journey)
  MUST cite the DataKnowledge / journey closed set — not freely invented names
```

Violations:

- required MRC item has no covering case → `needs_fix`
- matrix references a missing case or a case in the wrong layer → `needs_fix`
- e2e_if_enabled item is skipped without explicit skipped_by_scope + reason → `needs_fix`
  (the author must either cover it or add the explicit reason). Escalate to human
  only when an explicit reason exists and conflicts with another frozen scope input.
- closed-category MRC key absent from `.aa/data-knowledge.yaml` / declared journey set
  **and** no already-existing matching
  `plans/data-knowledge.proposal.*.yaml` `discovered_candidates` entry: follow the
  same closed-key recipe as `aa-case-design`. When an explicit requirement or
  resolved Explore assertion intent supplies the oracle, the row may remain
  covered and `proposal.md` Data Needs records the missing vocabulary. Otherwise
  return bounded `needs_fix` instructing `aa-case-design` to mark that exact
  matrix row `skipped_by_scope`, clear `covered_by_cases`, add a precise
  `skip_reason`, and narrow any unsupported case assertion. Do not instruct
  `aa-case-design` to create a data-knowledge proposal; it is not an authorized
  case-design output.

Do not require or suggest `trace.minimum_required_coverage` in case YAML. Case
`trace` is a separate capability proof map whose keys must be exact graph-provided
typed capability leaves; MRC IDs and MRC keys belong only in
`trace/minimum-coverage-matrix.json`.

Treat the MRC document as a complete coverage inventory, not as an implicit
business oracle. Every advisory MRC item must have one row, but
`required: true` does not by itself define an expected product behavior:

- When an explicit requirement or a resolved Explore `assertion_intent` fixes
  the expected behavior, require a covering case and use `needs_fix` for a
  missing row, fixture, step, assertion, or mapping.
- When a scenario was introduced only by advisory expansion, lies outside the
  explicit owner requirement, and has no frozen oracle, its exact matrix row is
  `skipped_by_scope` with an empty case list and a precise reason. If the author
  omitted that row or invented an assertion, return bounded `needs_fix` to add
  the skipped row or narrow the unsupported assertion. Do not create a blocking
  `needs_review` item and do not ask a human to define optional new behavior.
- Use `needs_human_review` only when the explicit owner requirement itself
  requires the ambiguous behavior and available frozen inputs/source do not
  select one meaning.

A matching `discovered_candidates[].knowledge_key` authenticates proposed
closed-category vocabulary for an MRC row. It does not promote that key into an
L1 capability leaf. Never require a proposed MRC key in case `trace`; verify
case trace only against the graph-provided typed L1 leaf set.

`case-review.json` should include:

```json
"minimum_coverage": {
  "total_required": 24,
  "covered": 24,
  "skipped_by_scope": 0,
  "missing": []
}
```

These four fields are an exact projection of the frozen matrix, not an estimate:

- count only rows whose `required` value is true;
- `covered` is the number of required rows with `status: covered`;
- `skipped_by_scope` is the number of required rows with that status;
- `missing` lists every required `skipped_by_scope` row's `key`, in matrix order.

The runtime recomputes this projection from `trace/minimum-coverage-matrix.json`
and rejects the review when any count or key differs.

---

## Review Criteria

### 1. Requirement Coverage

Check whether the cases cover the intended requirement.

Look for:

- Missing core business scenarios
- Missing positive path
- Missing negative path when relevant
- Missing permission / role scenarios when relevant
- Missing boundary conditions
- Missing regression impact
- Out-of-scope scenarios mixed into the case

If a requirement is ambiguous, do not invent product behavior. Mark it as human review required.

This ambiguity rule does not override frozen Explore intent. When the authenticated
Explore advisory answers an open question with `assertion_intent: assert_ideal`,
the ideal invariant named by that priority hint is the expected behavior for case
design even when current source violates it. Treat that source mismatch as the bug
the later execution/issue-analysis flow is meant to detect; do not route it to
human review and do not ask case-design to preserve the current faulty behavior.

### 2. Case Clarity

Each case should have clear:

- `case_id`
- `title`
- `summary`
- `module`
- `requirement_id`
- `feature_name`
- `priority`
- `severity`
- `type`
- `status`
- `preconditions`
- `test_data`
- `steps`
- `assertions`
- `automation` block (see YAML structure below)

The case should be understandable by a QA engineer without reading hidden context.

### 3. YAML Structure

Check that case files are valid YAML and follow the delta format produced by `aa-case-design`.

**Change delta files** (`qa/cases/**/case.yaml`) must use the delta top-level structure:

```yaml
schema_version: "1.0"

added:
  - case_id: "TC_USER_AUTH_001"
    title: "..."
    # ... all required case fields
modified:
  - case_id: "TC_USER_AUTH_002"
    title: "..."
    # ... all required case fields
removed:
  - case_id: "TC_USER_AUTH_003"
```

Review every case under `added` and `modified`. Review every entry under `removed`.

Do not expect a top-level `case_id` in the change delta file — cases are always nested inside `added`, `modified`, or `removed`.

**Required fields per case under `added` / `modified`:**

`CaseYamlAuthoring is the sole required-field source`; do not import required
fields from legacy case examples. In particular, `tags` is not required,
`framework` cannot be null, and `trace` must be non-empty.

```yaml
case_id: "TC_USER_AUTH_002"          # non-empty letters/digits/underscores only
title: "..."
status: draft|active|deprecated
priority: P0|P1|P2|P3
severity: blocker|critical|major|minor
type: API|E2E|Fuzz|Performance
module: "..."
requirement_id: "..."
feature_name: "..."
test_condition_id: "..."               # required (TBD acceptable with rationale)
design_technique: "..."                 # non-empty test-design description
objective: "..."
summary: "..."
preconditions: []
test_data: []
steps: ["at least one step"]
assertions: ["at least one observable assertion"]
postconditions: []
edge_cases: []
related_cases: []
risk:
  likelihood: 1–5
  impact: 1–5
  level: low|medium|high|critical
  rationale: "..."
regression:
  candidate: true|false
  tier: smoke|sanity|regression|full|none
  selection_reason: []
  maintenance_rule: "..."
  rationale: "..."
automation:
  required: true|false
  # no `target` field — top-level `type` (API|E2E|Fuzz|Performance) is the single source of truth
  framework: pytest|pytest-playwright|schemathesis|locust
  status: not_automated|planned|automated|flaky|deprecated
  confirmed_by: user|null                                  # optional; omission means null
  confirmed_at: <ISO-8601|null>                            # optional; omission means null
  fuzz: { endpoints: [{method, path}], property, expectations: [...] }    # only when type: Fuzz
  performance: { scenario: { capability, endpoint: "GET /path", thresholds, load } } # only when type: Performance
trace:
  <exact graph-provided typed capability leaf>: {covered: true}
```

**`automation_targets` field:** For change delta files produced by `aa-case-design`, `automation_targets` is **forbidden** and must be flagged as a blocker. Legacy stable case files in `qa/cases/**` may use it for reference, but the change delta under `qa/cases/**/case.yaml` must use the `automation` block.

### 4. Testability

Flag cases that cannot be implemented because:

- Steps are too vague
- Assertions are not observable
- Preconditions are missing
- Required data state is undefined
- Auth state is unknown
- Page route is unknown
- API endpoint is unknown
- Selector or UI behavior is unknown
- Expected result is subjective or not verifiable

### 5. Automation Readiness

For E2E cases, check:

- Natural language steps are executable
- Page entry point is clear
- User role is clear
- Test data is available or constructible
- Assertions can be checked with Playwright or pytest
- No hidden manual-only step is required

An authenticated graph capability plus DataKnowledge entry is constructible test
evidence. In particular, when `auth.e2e_limited_user_login` and
`capabilities.adapters.e2e.auth.seed_limited_user` are provided, a limited-user
precondition may rely on those declared factories and navigate directly to the
known page route before asserting permission-directed action visibility. Exact
role/menu/API binding mechanics belong in E2E planning and code generation. Do not
escalate that case to human review merely because the product component does not
itself define the test fixture.

For API cases, check:

- The case describes the scenario clearly (not necessarily the exact endpoint — endpoint detail belongs in the API plan, not the case)
- Preconditions and auth requirements are stated at a conceptual level
- Expected behaviour / response outcome is clear
- Test data requirements are described
- Do **not** flag a case as not automation-ready just because it omits method/path — that detail is added during API planning, not case design

### 6. Data and Preconditions

Check whether `test_data` and `preconditions` are enough.

Flag:

- Unknown fixture
- Unknown factory
- Missing cleanup strategy
- Conflicting data state
- Environment-specific assumptions
- Hardcoded production-like IDs
- Data dependencies that require manual preparation

### 7. Assertions

Assertions should be specific and observable.

Good assertions:

- Response status equals expected value
- Response body contains expected field/value
- UI displays expected text/state
- Database or state transition is verifiable if project supports it

Bad assertions:

- "System works correctly"
- "Page should be normal"
- "User experience is good"
- "No bug appears"

### 8. Traceability

Check whether the case links back to:

- Requirement ID
- Feature name
- Module
- Source document or user request if available
- Related existing case if modified

### 9. Forbidden Content

Case YAML must NOT contain any of the following. Flag as a blocker if found:

Fuzz and Performance are the field-level exception to the general rule below:
`automation.fuzz.endpoints[]` must contain concrete `method` + absolute `path`
objects and `automation.fuzz.property` plus non-empty `expectations` are required.
`automation.performance.scenario.endpoint` must identify the concrete HTTP
operation as `METHOD /absolute/path`, together with an exact capability leaf,
explicit positive load, and absolute thresholds. Do not flag those schema-owned
fields as forbidden execution detail.

- HTTP method or endpoint path outside the schema-owned Fuzz/Performance fields above
- `Authorization` header values (e.g. `Bearer ${token}`)
- Concrete request URLs with environment host (e.g. `https://prod.example.com/...`)
- Hard-coded auth tokens, real credentials, or secrets
- pytest code (`def test_`, `assert`, `@pytest.fixture`, `httpx`, `requests`)
- Playwright code (`page.locator(...)`, `await page.click(...)`)
- CSS selectors (`.btn`, `#form`, `.class-name`)
- XPath (`//button[@class='receive']`)
- `data-testid` attributes
- Raw SQL data setup
- Execution history or test run results embedded in the case body

These details belong in API plan, test code, or data-knowledge.yaml — not in case.yaml.

### 10. Delta Operation Correctness

For every change delta file (`qa/cases/**/case.yaml`), verify:

- `added[].case_id` **MUST NOT** already exist in the target stable case file (`qa/cases/<module>/case.yaml`). Violation = blocker.
- `modified[].case_id` **MUST** already exist in the target stable case file. Violation = blocker.
- `removed[].case_id` **MUST** already exist in the target stable case file. Violation = blocker.
- A `case_id` **MUST** appear in exactly one of `added`, `modified`, or `removed`. Duplication across lists = blocker.
- If the target stable case file does not exist yet, all cases **MUST** be under `added`. Any `modified` or `removed` entries are blockers.

If the target stable case file cannot be found, record as a warning (not a blocker) — it may not yet exist for a new module.

### 11. Duplicate and Conflicting Cases

- Check for cases with the same functional scenario (different IDs but identical intent).
- Check for cases whose assertions contradict each other.
- Check for cases that overlap with existing stable cases in `qa/cases/<module>/case.yaml`.
- Flag duplicates as auto-fixable (rename/merge) if safe; flag contradictions as `needs_human_review`.

### 12. Risk and Ambiguity

- Check whether `risk.level` is consistent with `priority` (P0/P1 → high/critical).
- Check whether high/critical risk cases have `automation.required = true`.
- Check whether `risk.rationale` is meaningful (not a generic placeholder).
- Flag vague risk rationale as low/medium finding. Flag missing risk block as blocker.

### 13. Layering Review (four-layer)

Compare `proposal.md` `## Layer Rationale` entries against each case's `type` (the single source of truth; there is no `automation.target`).

Flag these anti-patterns:

**API / E2E (functional layering):**
- A case verifiable by a single HTTP request is assigned `E2E` instead of `API` → **Type 1**
- Error-code / field-validation matrix is in E2E → **Type 1**
- Full CRUD flow uses E2E for every scenario (no API cases) → **Type 1** per scenario, or **Type 2** if assertions must migrate
- The same assertion point appears in cases of two different `type` → **Type 2** (assertion migration)

**Fuzz selection:**
- A `type: Fuzz` case has no `related_cases` pointing at a functional (API) case, i.e. Fuzz is used to replace functional assertions → **Type 2** (`human_review_required`, return to `aa-case-design`)
- `type: Fuzz` assigned to a pure read endpoint with no user input → **Type 1**, `fix_scope: ["type", "automation.fuzz"]`

**Performance selection:**
- `type: Performance` assigned to a low-frequency / non-core endpoint → **Type 1**, `fix_scope: ["type", "automation.performance"]`
- `type: Performance` case missing `automation.performance.scenario.thresholds` (p95_ms / error_rate_max) or using placeholder thresholds → **Type 2** blocker (thresholds must be user-confirmed)

**Classification:**

**Type 1 — Mechanical fix** (target label is wrong; assertions unchanged):
- Examples: error-code matrix assigned `E2E`; Fuzz on a no-input read endpoint
- `severity: low | medium`
- `auto_fix_allowed: true`
- `fix_scope: ["type"]` (plus `["automation.fuzz"]` / `["automation.performance"]` when the target sub-block must change)
- Reviewer MUST include `fix_scope` for every layering finding with `auto_fix_allowed: true`

**Type 2 — Design-level issue** (case must be split / refactored / assertions migrated, or Fuzz/Performance misused):
- Examples: one E2E scenario wraps both API logic and UI interaction; Fuzz replacing functional assertions; Performance without thresholds
- `severity: high`
- `auto_fix_allowed: false`
- `human_review_required: true` (workflow: return to `aa-case-design` for redesign)

Finding example (Type 1):

```json
{
  "id": "CASE-FINDING-LAYER-001",
  "severity": "medium",
  "category": "layering",
  "file": "qa/cases/<module>/case.yaml",
  "message": "TC_MENU_001 validation is verifiable via single API request; set type to API not E2E.",
  "suggestion": "Change type from E2E to API.",
  "auto_fix_allowed": true,
  "fix_scope": ["type"],
  "human_review_required": false
}
```

---

## Decision Rules

Return one of these decisions:

- `pass`
- `needs_fix`
- `needs_human_review`
- `reject`

**Use `pass` when:**

- Case artifacts are complete enough.
- No blocker exists.
- Planning/codegen can continue safely.

**Use `needs_fix` when:**

- Issues are clear and can be automatically fixed.
- No product decision is required.
- No high-risk ambiguity exists.
- The requirement, frozen artifacts, and independently read product source make the intended
  behavior unambiguous, and the repair is a bounded edit to the case/proposal artifacts that the
  independent `aa-case-design` agent can apply from explicit locators and instructions.
- The case claims a behavior but omits the directly required fixture, step, or assertion. For
  example, when a source-verified parent/child tree assertion has only root test data, adding a
  child fixture plus the matching steps and assertions is a `needs_fix` repair; it does not require
  a person to rediscover already-proven behavior.
- Severity alone does not require human review. A high-severity finding may still be mechanically fixable
  when its desired result and exact edit scope are already proven. In that situation you
  **must use `needs_fix`**, populate `auto_fix_plan`, and route back to the independent case-design
  agent.

**Use `needs_human_review` when:**

- Product behavior is ambiguous.
- Test scope needs confirmation.
- Auth/role/data behavior is unknown.
- A missing requirement cannot be inferred from files.
- The required repair has two or more plausible product meanings and the available evidence does
  not select one. High or critical risk by itself is not such an ambiguity.

An optional assertion invented by the case author is not, by itself, a reason to
ask a person to define new business behavior. When that assertion is not required
by the requirement or frozen MRC, return `needs_fix` with the bounded repair
"remove or narrow the unsupported assertion". Human review is reserved for an
ambiguity that blocks an explicitly required scenario.

**Use `reject` when:**

- Case artifacts are fundamentally wrong.
- The cases test the wrong feature.
- YAML is invalid and cannot be safely repaired.
- Generated cases conflict with explicit requirement.

**Missing Inputs (special case):**

If required input files are missing, STOP before normal review — do not produce a normal review verdict. If the orchestrator requires a JSON artifact for diagnostics, write a minimal `reject` record:

```json
{
  "decision": "reject",
  "risk_level": "critical",
  "human_review_required": true,
  "auto_fix_allowed": false,
  "next_action": "stop",
  "blockers": [{ "id": "CASE-BLOCKER-MISSING", "severity": "critical", "message": "Required file missing: <path>", "required_action": "Provide the missing file and re-run aa-case-reviewer." }]
}
```

Default behaviour is STOP; the JSON artifact is written only to enable orchestrator diagnostics, not to trigger a fix loop.

### Decision ↔ JSON Field Constraints

These field values **must always be consistent**. Violating them creates gate bypass risk:

| decision | human_review_required | auto_fix_allowed |
|---|---|---|
| `pass` | `false` | `false` |
| `needs_fix` | `false` | `true` |
| `needs_human_review` | `true` | `false` (MUST be false) |
| `reject` | `true` | `false` (MUST be false) |

**Rule**: `human_review_required` MUST be `true` whenever `decision` is `needs_human_review` or `reject`.
**Rule**: `auto_fix_allowed` MUST be `false` whenever `human_review_required` is `true`.
**Rule**: `next_action` MUST match `decision`:

| decision | next_action |
|---|---|
| `pass` | `continue` |
| `needs_fix` | `run_case_design` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

A reviewer that writes `decision = "needs_human_review"` with `human_review_required: false` produces an invalid review that enables fixer gate bypass.
A reviewer that writes `decision = "needs_fix"` with `next_action = "continue"` produces a contradictory gate that would skip required fixes.

### needs_fix Hard Rules

When `decision == "needs_fix"`, ALL of the following MUST hold — violating any is an invalid review:

- `auto_fix_allowed` MUST be `true`.
- `human_review_required` MUST be `false`.
- `auto_fix_plan` MUST contain at least one item.
- Every `auto_fix_plan[].finding_id` MUST reference an existing entry in `findings`.
- Every referenced finding MUST have `auto_fix_allowed == true`.
- Every referenced finding MUST have `human_review_required == false`.
- Findings with `severity == "critical"` MUST NOT be referenced in `auto_fix_plan`.
- A `high` finding MAY be referenced only when requirement/product-source evidence proves one
  intended repair and the plan names a bounded artifact locator and exact fields or steps to edit.
- Every automatic repair artifact must be an authorized case-design output:
  `proposal.md`, `trace/minimum-coverage-matrix.json`, or one of the
  graph-provided exact `case_delta_paths`. A file under `plans/`, `.aa/`, the
  stable `qa/cases/` tree, or any other path cannot appear as an automatic
  repair target. Route the repair through an authorized case/proposal/matrix
  edit or do not classify it as mechanically auto-fixable.
- Every `auto_fix_plan` item must use this exact shape; do not substitute
  `action`, `instructions`, or a free-form `locator` for these fields:
  `{"finding_id":"CR-001","artifact":"qa/cases/<module>/case.yaml","case_id":"TC_001","edits":["one exact edit instruction"]}`.
  Use `case_id: null` for a proposal or matrix repair. The matching
  finding locator's `key` is the exclusive field or section scope.
- Each `auto_fix_plan[].artifact` MUST equal that finding's `locator.artifact`,
  and each finding ID may appear in exactly one plan item. If one logical defect
  requires edits to two artifacts, split it into one finding per artifact, give
  each finding its own exact locator and unique ID, and reference each ID in one
  matching plan item. Never reuse a case-file finding ID for a `proposal.md`
  edit, or vice versa.
- For a `case.yaml` finding, `locator.key` MUST contain only dotted field paths.
  Use one path such as `"key":"automation.e2e.entry"`, or for multiple sibling
  fields use `"key":"steps,assertions"` with a comma separator. The form
  `"key":"steps/assertions"` is invalid; `/`, `|`, prose, JSONPath, and YAML
  selectors are not accepted by the repair contract.
- When the one proven repair is to add an entire missing current-change case,
  keep its intended `case_id` in both the finding and plan and use `added` as
  `locator.key`. When the repair is to remove an entire current-change case, keep
  its exact `case_id` and use the exact current delta section: `added` or
  `modified` as `locator.key`. Never use `case_id` as `locator.key`: `case_id`
  identifies the case; it is not a mutable field scope. Do not use this structural
  authorization to move, reorder, or rewrite another case, or to remove a case
  required by the owner scope or frozen Explore oracle.
- For a `proposal.md` finding, `locator.key` MUST be one complete, unique level-two
  ATX heading exactly as written in the file, including the `## ` prefix (for
  example `"key":"## Data Needs"`). A bare title, heading fragment, line range,
  or higher/lower-level heading is invalid. The repair may change only that
  section body and may not add, remove, rename, or reorder headings.
- For `trace/minimum-coverage-matrix.json`, `locator.key` MUST contain only exact
  `mrc_id` values separated by commas and listed in their current matrix order
  (for example `"key":"MRC-DATA-001,MRC-DATA-002"`). A field selector such as
  `MRC-DATA-001.status` is invalid. The repair may change only
  `status`, `covered_by_cases`, and `skip_reason` on those rows; row identity,
  order, key, required flag, category, and layer are frozen.
- `.qa.yaml` contains change identity, targets, and approval authority and MUST
  NOT appear in `auto_fix_plan`. Report such a finding as diagnostic and choose
  `needs_human_review` or `reject` when it blocks progress.

---

## Risk Level Rules

Use: `low` / `medium` / `high` / `critical`

- **low**: Minor clarity or formatting issues.
- **medium**: Missing optional edge case. Some assertions could be stronger. Minor data setup uncertainty.
- **high**: Missing core scenario. Product behavior ambiguous. Auth/role/data setup unknown. Codegen likely to generate invalid tests.
- **critical**: Wrong feature. Invalid or missing required files. Case contradicts requirement. Dangerous destructive behavior is proposed.

---

## Auto Fix Rules

Set `auto_fix_allowed = true` only when all fixable findings can be applied mechanically.

Examples of auto-fixable findings:

- Missing tag
- Inconsistent status value
- Weak but obvious wording improvement
- Missing trace field when source is known
- YAML formatting issue
- Duplicate title that can be renamed safely
- Missing assertion that is directly implied by a step
- Missing fixture or step needed by an existing assertion when the requirement and independently
  verified product source prove the intended behavior and the affected case locator is exact

Set `human_review_required = true` when:

- Product behavior must be decided
- Unknown route/auth/role/data must be confirmed
- Test scope is disputed
- A requirement interpretation is uncertain
- The fix would require inventing business behavior

Severity alone does not require human review. Choose the route from evidence ambiguity and repair
scope, not from the highest finding severity.

---

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `assurance_intake.contracts` (`CaseReviewResultV1`). Runtime `finalize` validates
> authored files against that model. The example below is illustrative only.

Write valid JSON to:

```text
qa/results/review/case-review.json
```

**Minimal top-level structure (illustrative — see `assurance_intake.contracts` (`CaseReviewResultV1`) for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "case",
  "change_id": "<change-id>",
  "decision": "pass",
  "risk_level": "low",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "findings": [
    {
      "id": "CR-001",
      "severity": "high",
      "category": "coverage",
      "message": "required case is missing a locator-backed assertion",
      "locator": {"artifact": "change:cases/module/case.yaml", "case_id": "TC_001"}
    }
  ],
  "auto_fix_plan": [],
  "next_action": "continue",
  "minimum_coverage": {
    "total_required": 2,
    "covered": 2,
    "skipped_by_scope": 0,
    "missing": []
  },
  "source_verification": {
    "independent": true,
    "reviewed_source_files": ["src/app.py"],
    "verified_claims": [
      {"claim": "create item persists a menu record", "evidence_files": ["src/app.py"]}
    ]
  }
}
```

`reviewed_files` MUST include:
- `qa/proposal.md`
- `qa/.qa.yaml`
- every `qa/cases/**/case.yaml` reviewed

`reviewed_files` MUST NOT be empty.

`fix_scope` is REQUIRED when a finding has `category == "layering"` and `auto_fix_allowed == true`.
It lists the exact case fields the fixer is permitted to touch.
For all other categories, `fix_scope` is omitted.

**`needs_review` hard rules:**

- If `needs_review` is non-empty and any item has `blocking == true`:
  - `decision` MUST be `needs_human_review`.
  - `human_review_required` MUST be `true`.
  - `auto_fix_allowed` MUST be `false`.
  - `next_action` MUST be `human_review`.
- If all items have `blocking == false`, a `pass` or `needs_fix` decision is still allowed, but the items must remain in `needs_review` so the reviewer record is transparent.

---

## Summary Markdown Format

Write a human-readable summary to:

```text
qa/results/review/case-review-summary.md
```

Use this structure:

```markdown
# Case Review Summary

## Decision

- Decision:
- Risk:
- Auto Fix Allowed:
- Human Review Required:

## Summary

...

## Blockers

...

## Findings

...

## Auto Fix Plan

...

## Next Action

...
```

---

## Final Response

After writing both files, re-open `case-review.json` and parse it. Return the complete parsed JSON object
as the final assistant response. The response must
be exactly the same object written to `case-review.json`, including every field
required by the injected result contract. Do not wrap it in a Markdown fence or
add prose before or after it.

Do not return only the routing fields (`decision`, `risk_level`,
`human_review_required`, `auto_fix_allowed`, `next_action`). The graph needs the
complete review object even though the same object also exists on disk.

Do not claim the review passed unless `decision = pass`.

Do not modify case files in this skill.
