# SDD Progress — Levers Remaining Slices

**Plan:** docs/superpowers/plans/2026-08-15-levers-remaining-slices.md
**Branch:** chore/slimming-dead-paths
**Start HEAD:** d2dcf7f

## Tasks
1: complete (commits d2dcf7f..af29198, review clean)
2: complete (commits af29198..0e37367, review clean)
3: complete (commits 0e37367..d177795, review clean)
4: complete (commits d177795..187bf3b, review clean)

## Task 1 review
- Reviewer: Approved (spec ✅, no Critical/Important)
- Minors for whole-branch triage:
  - No unit test that a failed collect is returned unmerged (`test_run_tests_and_collect.py`)
  - `graph_ops.py` imports metrics (plan-mandated; domains cycle with existing metrics→execution)
  - Stale test names in `test_metrics_sufficiency_gate.py` still say “after collect”
  - Dead stub mappings still register unused `operation:collect-pr-metrics-batch`

## Task 2 review
- Reviewer: Approved (spec ✅, no Critical/Important)
- ⚠️ dual-source loop: controller confirmed it calls the original op with a synthetic node_id and does not assert inspect topology — no schema update required
- Minors for whole-branch triage:
  - Composite glue tests monkeypatch both callees (no real-handler write)
  - No unit test that a failed gaps step is returned unmerged
  - `test_the_materialize_node_publishes_both_documents` still pins TARGET contract reads, not the composite union

## Task 3 review
- Reviewer: Approved (spec ✅, no Critical/Important)
- ⚠️ live `aa workflow supersede` now always sees an allow decision (`not_legacy_blocked`); brief required wrapper no-op + unchanged eligibility — CLI kept, not live-eligible. Controller: by design, not a miss.
- Minors for whole-branch triage:
  - `test_receipt_exact_replay_is_idempotent` second evaluate is vacuous (evaluate ignores existing_receipt)
  - `evaluate_resume_compatibility` docstring still mentions exact receipt reuse
  - `_selected_layers` unused after evaluate-body delete (keep with unused audit helpers)
  - Unused runtime helpers `_load_topology_compatibility_receipt` / `_legacy_profile_reconstructable` (brief: keep unused)

## Task 4 review
- Reviewer: Approved (spec ✅, no Critical/Important)
- ⚠️ `GraphProjection.state_values` producer: controller confirmed `checkpoint.py` fold copies `SuperstepCommittedEvent.state_values`
- ⚠️ `aa decide` / configure / skills: controller confirmed `record_decision` and `configure_workflow_params` still exist and were not migrated
- Minors for whole-branch triage:
  - Finalize tests the helper with a constructed projection, not `_attach_gate_report` fold-success vs `LedgerIntegrityError`
## Whole-branch review
- Reviewer: Ready to merge? Yes. No Critical/Important.
- New minors (do not block): stale supersede receipt comment (`runtime.py:364`); nightly schema comment/`max_supersteps: 14`; `test_pre_task13_resume_does_not_upgrade_v1_or_inject_materializer` name vs fail-closed body.
- Operator note: in-progress invocations pinned to the pre-fold graph cannot resume after deploy (`graph_definition_changed`).

