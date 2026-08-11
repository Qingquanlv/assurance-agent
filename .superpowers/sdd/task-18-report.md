# Task 18 Report — Dark-Ship Selected-Test Behavior and Current-Chain Scoring

## Status

**DONE** (edit + test only; no git add/commit — controller owns commits)

## Files Changed

```
assurance_agent/verification/generated_entries.py
assurance_agent/eval/scorers/current_codegen.py
assurance_agent/eval/scorers/shared.py
tests/unit/verification/test_generated_entries.py
tests/unit/eval/test_codegen_scorer.py
tests/unit/eval/test_scorers.py
tests/unit/verification/test_fuzz_performance_contract_fixtures.py
tests/unit/workflow/graph/test_task_input_snapshot.py
.superpowers/sdd/task-18-report.md
```

## What Landed

1. **Behavioral mapping inputs** — `LayerBehavioralPolicy` attached to `LayerMappingRelation` (closed client/schema/user/task/navigation forms). Plan wire shape unchanged; fail-closed mapping errors retained.
2. **Four AST classifiers** — `classify_api_entry` / `classify_e2e_entry` / `classify_fuzz_entry` / `classify_performance_entry` (+ dispatcher) emitting `GeneratedEntryDecision`. Independent positive/negative corpora cover behaviorless and incomplete shapes; registered forms have direct mutation coverage.
3. **Dark current-chain scorer** — `assurance_agent/eval/scorers/current_codegen.py` with `CurrentLayerBinding` / `CurrentLayerCodegenEvidence`, epoch-closed chain evaluation, receipt/write attribution, and callable calculations for `current_assurance_chain_rate`, `current_codegen_attempt_rate`, `selected_test_write_rate`. Not registered on live `codegen.py`.
4. **Policy replay** — `shared.replay_policy_integrity` reconstructs `WritePolicyV1` from envelope selected layers and requires byte-identical persisted policy before write credit; failure zeros all three hard metrics.
5. **D13 inventory** — scorer snapshot/context reads (`input_snapshot_id`, `runtime_context_sha256`) added to the closed AST consumer inventory.

## Verification

```text
uv run pytest -q \
  tests/unit/verification/test_generated_entries.py \
  tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
  tests/unit/eval/test_codegen_scorer.py \
  tests/unit/eval/test_scorers.py \
  tests/unit/workflow/graph/test_task_input_snapshot.py
→ 88 passed

uv run ruff check assurance_agent/verification/generated_entries.py \
  assurance_agent/eval/scorers \
  tests/unit/verification/test_generated_entries.py \
  tests/unit/eval/test_codegen_scorer.py
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations
```

## Preserved

- Live `eval/scorers/codegen.py` registration and syntax/secret/summary/evidence metrics.
- No dataset/suite requests the three hard metrics (Task 22 activation).

## Suggested Commit (controller)

```text
feat(eval): stage current behavioral codegen evidence
```
