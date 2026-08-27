# Phase 6 handoff — Phase 5 product assembly (initial freeze stub)

This file is frozen at Task 1. Phase 6 owns cutover, drain, and deletion. Task 27 completes the machine-checkable handoff per spec Section 25.

## Freeze status

- `compatibility_bridge_allowed`: false
- `status`: stub_pending_task_27

## Required Phase 6 actions (closed set)

1. `freeze_new_legacy_starts`
2. `drain_or_audit_terminate_legacy_invocations`
3. `switch_aa_to_assurance_product`
4. `remove_aa_next_name`
5. `decide_and_implement_safe_in_place_result_application_if_required`
6. `delete_legacy_runtime_and_product_implementation`
7. `delete_product_hooks_catalogs_old_entrypoints_resources_and_obsolete_tests`
8. `remove_comparison_only_compatibility_baseline_where_unneeded`
9. `add_no_old_invocation_resume_adapter_or_forwarding_import`

## Residuals allowed until Phase 6

See `residuals.yaml`: legacy `aa` command, legacy runtime, legacy product, and comparison baseline only.

## Deliverables pending Task 27

Product/source digests, entry-point coordinates, six-wheel constraints, deployment builder identity, 99-binding report, comparison matrix evidence, provider-live benchmarks, old-invocation disposition, command mapping, deletion inventory, and no-import proof.
