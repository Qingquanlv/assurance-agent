# Task 13 Report — Clean-cut guards, E2E acceptance, documentation

**Status:** DONE  
**Base:** `4ccacc7`  
**Commits:** `96b485e` (acceptance/docs/guards) · `343b6be` (report SHA)

## Deliverables

1. **Production guard** — `tests/unit/retro/test_no_historical_retro_reads.py`
   - Forbidden tokens: `_state.json`, `cross-run-report.json`, `promotions.json`, `"workflow_bug"`, `"issue_export"`
   - `skill:aa-retro` contract/SKILL narrow-read assertions
   - AST scan for `iterdir`/`glob`/`rglob` rooted at `qa/retro`

2. **Offenders removed (Task 12 carry-over)**
   - Deleted legacy `RetroProposal` / `LegacyRetro*` / `workflow_bug` / `issue_export` from `assurance_agent/retro/types.py`
   - Removed dead absorb kwargs (`is_terminal`/`context_builder`, `min_evidence`/`rework_alert`)
   - Removed nightly wording in collect/accept/retro_ops/operation/host_runner
   - Deleted unused `tests/unit/retro/proposal_fixtures.py`

3. **Six E2E acceptance scenarios** — `tests/integration/test_retro_issue_improvement_acceptance.py`
   1. Full workflow product_bug; Retro/Improvement absent from full closure
   2. Same misclassification → one `prompt_improvement` (no new Problem)
   3. `workflow_issue` then separate `workflow_improvement`
   4. Same intent across Retro IDs → one Improvement + `improvement_evidence_linked` (amendment 4A)
   5. Apply Improvement leaves Problem bytes unchanged
   6. Knowledge export requires resolved/human_confirmed/verified; L1 identical before promote

4. **Graph-order regressions** — packaged `assurance`/`healing` edges + issue lifecycle acceptance/workflow tests

5. **Docs** — `docs/schemas.md`, `docs/eval.md`, `README.md` document schema-v2 Retro + Improvement cut; remove nightly/export-issues guidance; document `aa improvement`

## Gate evidence

| Gate | Result |
|---|---|
| Focused acceptance suite | pass |
| `uv run pytest -q` | **1958 passed, 2 skipped** |
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass (also reformatted prior-task drift) |
| `uv run pyright` | 0 errors |
| `uv run lint-imports` | 6 kept, 0 broken |
| `bash scripts/packaging_smoke_test.sh` | OK (skill count 32→34 for `aa-issue-*`) |

## Extra fixes required for green gates

- `pyproject.toml`: `addopts = ["--import-mode=importlib"]` — sibling `tests/unit/workflow/{issues,improvements}/test_*.py` basename collisions
- `scripts/packaging_smoke_test.sh`: expect 34 packaged skills (includes issue analyzer/triage)
- Repo-wide `ruff format` for pre-existing format debt from Tasks 1–12

## Not staged

- `benchmark/vue-fastapi-admin/**` dirt (advisory.json + loop log)

## Final checklist

- [x] Issue full-workflow / healing order unchanged except explicit order tests
- [x] Retro has no full-workflow edge
- [x] Production historical Retro reads / legacy enums = zero
- [x] Fingerprint merge across Retro IDs via `improvement_evidence_linked`
- [x] Knowledge export never mutates L1 before promote
- [x] All listed gates green

## Review fixes (post-`343b6be`)

Closed the three Important findings from `task-13-review.md` so Quality can Approve.

1. **Scenario 1 — real Issue ops create `product_bug`**  
   Path chosen: direct product `collect_observations` → scripted analyze artifacts → `reconcile_issues` (not hand-written `problems.json`; not full agent GraphRuntime). Asserts `problem_detected` + `product_bug` classification, no `qa/retro/**` / `qa/improvements/**`, plus packaged full-closure topology excludes Retro/Improvement graphs and ops.

2. **AST walk guard — Name-bound `qa/retro` roots**  
   `_QaRetroWalkVisitor` resolves in-function Name bindings (and chained `qa` then `retro` assignments). Requires both path segments (or `"qa/retro"`) to avoid false positives on `qa/changes` / `qa/cases`. Exposed `scan_qa_retro_walks()` with synthetic Name-bound / chained unit pins.

3. **Scenario 6 — empty `verification_scope` negative pin**  
   Resolved + `human_confirmed` Problem with empty `verification_scope` (via `model_construct`) raises `verified resolution scope`; L1 bytes unchanged.

**Focused gate:**  
`uv run pytest tests/unit/retro/test_no_historical_retro_reads.py tests/integration/test_retro_issue_improvement_acceptance.py -q` → **12 passed**
