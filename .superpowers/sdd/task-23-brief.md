### Task 23: Publish Compatibility Semantics and Run the Release Gate

**Files:**
- Modify: `docs/schemas.md`
- Modify: `README.md`
- Modify: `docs/eval.md`
- Create: `docs/release-notes/2026-08-four-layer-assurance.md`
- Create: `tests/unit/test_docs_contract.py`

**Interfaces:**
- Produces: operator/user documentation for v6 bindings, declared-only inputs, generated-file authority, durable effects, legacy resume narrowing, supersede, multi-layer codegen-only, D17 evidence, and hard benchmark metrics.
- Consumes: final implemented CLI help, wire models, reason codes, and suite schemas.
- Preserves: no documentation promise exceeds tested OpenCode configuration/request binding; third-party sandbox enforcement is explicitly outside CI proof.

- [ ] **Step 1: Add failing documentation-contract assertions**

  Require exact schema IDs, six v6 semantic fields, both validator IDs, three effect kinds, legacy block reason, supersede command/actions, four selected layer values/default, evidence-export algorithm, and three hard metrics. Reject stale text claiming single-layer codegen-only, summary-based authority, or automatic imported-codegen healing.
- [ ] **Step 2: Run the documentation test and observe missing contracts**

  ```bash
  uv run pytest -q tests/unit/test_docs_contract.py
  ```

  Expected: assertions fail until documentation is updated.
- [ ] **Step 3: Document compatibility and operational exits**

  State that v1-v5 remain parseable/displayable, topology receipts are not commit-safety proof, report-only legacy work may continue, pending assurance commit work stops, and `aa workflow supersede` is the sole audited rerun-v6/stop exit. Document intentional imported-codegen healing narrowing.
- [ ] **Step 4: Run all focused feature suites**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_topology_semantics.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_supersede.py \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_four_layer_resume.py \
    tests/integration/test_codegen_fixer_record.py
  uv run pytest -q tests/integration/test_eval_cli.py -k codegen
  ```

  Expected: all focused suites pass.
- [ ] **Step 5: Run the complete repository release gate**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run lint-imports
  uv run pytest -q
  bash scripts/packaging_smoke_test.sh
  ```

  Expected: all six commands pass without an OpenCode server.
- [ ] **Step 6: Run mechanical architecture and contract scans**

  ```bash
  ! rg -n 'workflow-state\.yaml|phases\.|aa heal record-apply' \
    assurance_agent/_resources/skills/aa-{api,e2e,fuzz,performance}-*
  ! rg -n 'events\.jsonl' \
    assurance_agent/_resources/skills/aa-api-plan-fixer \
    assurance_agent/_resources/skills/aa-e2e-plan-fixer
  uv run pytest -q \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py \
    -k 'declared_only_exact_count or exact_validator_contract_set or exact_effect_contract_set or consumer_set'
  ```

  Expected: forbidden token scans return no match; declared-only occurs exactly sixteen times; validator/effect consumer-set tests confirm exact six/four contract sets.
- [ ] **Step 7: Inspect final history and diff**

  ```bash
  git status --short
  git log --oneline --decorate -23
  git diff origin/main...HEAD --stat
  git diff origin/main...HEAD --check
  ```

  Expected: no unexpected tracked changes, no whitespace errors, and the commit sequence follows Tasks 1-23 with Task 15 as the runtime-contract activation and Task 22 as the later benchmark activation.
- [ ] **Step 8: Commit documentation after all gates pass**

  ```bash
  git add docs/schemas.md docs/eval.md docs/release-notes/2026-08-four-layer-assurance.md \
    README.md tests/unit/test_docs_contract.py
  git commit -m "docs(assurance): publish v6 runtime evidence semantics"
  ```
