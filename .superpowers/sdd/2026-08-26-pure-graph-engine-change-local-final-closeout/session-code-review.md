# Session Code Review — Tasks 11–14 Final Closeout

**Range:** `39143602eb8b2ee4b296b87ed2bb7f7bc9613f7c` .. `6740dce039592b40da003acb2e7aa1270f225423`
**Branch:** `codex/pure-graph-engine-phase3-spec`
**Plan:** `docs/superpowers/plans/2026-08-26-pure-graph-engine-change-local-final-closeout.md` Tasks 11–14
**Reviewer checkout:** read-only except this file
**Diff package:** `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/review-session-3914360..6740dce.diff`

This review covers the session after Task 10: product/benchmark rename, current docs and export acceptance, OpenCode live (blocked) plus the `resume_running` recovery seam, and the Task 14 evidence pack (`accepted_with_waivers`). Uncommitted leftover host/capability hunks are out of scope. Full ruff/pyright/pytest/product-smoke were recorded failing on the dirty tree; those suite failures are not re-opened here unless this range introduced them.

## Spec Compliance (session)

| Task | Verdict | Notes |
|---|---|---|
| 11 rename + delete comparison | Present | `tests/phase5` → `tests/product`, `benchmark/assurance-product-phase5` → `benchmark/assurance-product`; comparison executables and both fixture sides are gone; mapping test pins eight categories |
| 12 current docs + export acceptance | Present | Docs lock `aa run` → achieved → `aa export` → optional `aa archive` (justified deviation from the plan’s `aa workflow run` line; `aa workflow` is now a forbidden fragment). Export tests consume shipped `publish_achieved` / `archive_published` |
| 13 OpenCode live | Blocked, not fabricated | `final-opencode.json` is `admission_status=blocked`, `achieved` absent, `external_blocked=false`, `generation_execute_invalid_output` |
| 13 scheduler recovery | Present | Prepared-undispatched same-attempt dispatch; already-dispatched and cancel-requested stay on `activity_recovery` |
| 14 residual + evidence | Present as waiver pack | Zero `carried_forward`; OpenCode/Cursor rows deferred; verdict `accepted_with_waivers`; Step 4/5 left `pending_controller` |
| Global: no whole-tree / `deleted CLI alias` aliases / fabricated live / Change-local rewrite | Mostly held | Live not rewritten as achieved; frozen Phase 5 handoff and Change-local admission untouched. Final live runner still writes `deleted CLI alias` into evidence notes (Important 1) |

Original Completion Definition items 3, 7, and 8 (OpenCode live closed and `achieved`; all repository/type/import/security/packaging/wheel/no-legacy gates green) are **not** met. The pack states that. Controller rulings dated 2026-08-27 (`先完成task14`, do not wait for `achieved`, do not start or rewrite live) amend those sentences for this session; they do not convert the product `invalid_output` into an out-of-scope item.

---

### Strengths

- The `resume_running` change is small and local (`scheduler.py:599-608`, `:1646-1652`). It treats `prepared` + no dispatch fingerprint as “not live provider work,” reopens the persisted workspace and `activity_id`, and does not `begin()` a new attempt. Already-dispatched live activities still `continue`. That matches the stall the live run then re-checked: generation execute prepared and dispatched (`final-opencode.json:75-84`).
- Cancel-requested prepared activities are held on the skip (`scheduler.py:606-608`). Recover remains the only cancel/reconcile driver (`scheduler.py:410-417`). `test_cancel_requested_prepared_stays_activity_recovery` locks no `execute`, unchanged ledger, `terminal_reason == "activity_recovery"` (`test_activity_recovery.py:1083-1114`). The earlier Important from `c0b6393` is closed at `0f034de`.
- Recovery tests go through `Engine.run_until_blocked` after a real `start_recoverable` and reopen, not a mocked scheduler method. The happy path asserts the same `activity_id`, workspace identity, attempt `1`, one `task_attempt_started` / `task_activity_prepared`, and one host `execute` (`test_activity_recovery.py:1048-1080`). The already-dispatched counterpart only injects `TaskActivityDispatchStarted`.
- Task 11 rename is mechanical and complete for tracked consumers: smoke fixture paths, capability-package imports, `PHASE5_REPLACEMENT_TEST`, CI (no hardcoded `tests/phase5` / `assurance-product-phase5`). Comparison-only surface is actually deleted, not aliased.
- Task 12 docs tests read the real current-doc files and lock ownership, plugin rules, the installed command list, and a closed forbidden-fragment list including `deleted CLI alias`, `workspace/trees`, `HEAD.json`, and `aa workflow` (`test_current_documentation.py:22-35,82-108`). graph-engine README is asserted *not* to claim `aa` ownership or the product delivery sentence.
- Export acceptance calls the shipped product APIs with the existing `write_achieved` fixture and covers the six named cases: non-achieved, target drift, declared file set, crash-then-retry, idempotency, archive-after-receipt (`test_export_delivery_acceptance.py:25-158`). Production export/archive code was not rewritten.
- Task 13/14 evidence is honest. `final-opencode.json` does not invent `achieved` or a publish receipt. `acceptance.md` leads with **not fully accepted**. `final-evidence.json` records non-zero gate exits, `opencode_live.achieved=false`, `step4_whole_branch_review=pending_controller`, and the return-to-owner list. Frozen `phase6-handoff.json` still says OpenCode `carried_forward` and was not edited. Dirty `production_host` / `production_worker` hunks were not staged.
- No-legacy allowlist additions are exact paths, no wildcards (`scripts/no_legacy_allowlist.txt`).

---

### Issues

#### Critical (Must Fix)

None.

#### Important (Should Fix)

1. **Final live runner still writes `deleted CLI alias` into evidence notes after the console binary was switched to `aa`.**
   - File: `benchmark/assurance-product/run_item.py:1`, `:266-281`, `:340-359`, `:1084`, `:1107`, `:1141`, `:1163`
   - Task 11 correctly points the isolated venv at `venv/bin/aa` and fails closed if that file is missing. The module docstring is still “driver through deleted CLI alias.” `_aa_next` / `aa_next` remain the helper and binding names. Operator-visible `finish(..., notes=...)` strings still say “terminal deleted CLI alias status,” “deleted CLI alias run exited non-zero,” “deleted CLI alias run returned a pending interrupt,” and “last deleted CLI alias run returned ….” Those notes land in live `evidence.json`.
   - Why it matters: the global constraint is “do not retain `deleted CLI alias`.” This is the post-cutover product benchmark path this session was asked to finish. `scripts/check_no_legacy.py` does not scan `benchmark/`, so the repository gate stays green while the live harness still emits the deleted CLI name. A later reader of a blocked run will think the isolated env still invoked `deleted CLI alias`.
   - Fix: rename the helper/variable to `aa`, rewrite the docstring and every `notes=` / detail string to `aa`, and keep the `venv/bin/aa` existence check.

2. **OpenCode live residuals are `deferred_out_of_scope` after an in-scope product fail-closed, not a quota/network external blocker.**
   - File: `tests/phase6/conformance.py:64,68,81`; `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/residual-disposition.json:4,9,22`
   - Latest record `063450-3e30e331` terminated `failed` at `generation-api-plan` execute with `invalid_output` (missing `case_ids`, `required_capabilities`, `coverage`, `output_files`). `final-opencode.json:86-87` sets `outcome=blocked` and `external_blocked=false`. Plan Task 13 Step 2 only allows quota/network to leave the task incomplete. Cursor live was the pre-approved `deferred_out_of_scope` class. The user instruction `先完成task14` is a sequencing waiver; it is not a scope change for a deterministic result-contract failure.
   - Why it matters: `EXPECTED_RESIDUAL_MAPPINGS` now authenticates Phase 3 OpenCode live, Phase 5 Task 24, and Phase 6 Task 15 as out of scope. Admission will stay green while Completion Definition items 3 and 7 stay unmet. A later owner will skip the generation execute contract the live run actually hit. The pack itself is honest (`achieved: false`); the residual *disposition* is the miss.
   - Fix: do not mark those rows `verified_complete`. If the residual enum cannot say `failed` / `blocked`, keep them deferred only with an explicit in-scope product-failure label that admission treats differently from Cursor live — or leave a user-approved waiver whose evidence names `generation_execute_invalid_output` as the open product defect, not “out of scope.” Do not rewrite `063450-3e30e331` as `achieved`.

#### Minor (Nice to Have)

1. **Comparison mapping test is an AST name check.**
   - File: `tests/phase6/test_final_benchmark_manifest.py:80-106`
   - It requires eight category keys and that named `FunctionDef`s exist. It does not pin comparison case ids and will stay green if those functions are emptied. The mapped homes themselves still assert product behavior (checked in the Task 11 review). Tighten later with case id → node id, or collect the mapped nodes through the existing gate tables.

2. **Export interruption recovery is white-box.**
   - File: `tests/phase6/test_export_delivery_acceptance.py:84-113`
   - The case monkeypatches `assurance_product.export._journal_cut`. Recovery after the crash does go through public `publish_achieved`, and production was not patched to add a new seam. The brief asked for black-box acceptance. The authenticated-manifest case also skips receipt digest bindings (change id / apply manifest / source and target digests) beyond `PublishReceiptV1` round-trip.

3. **`resume_running` skip predicate is dense and re-folds the ledger.**
   - File: `packages/graph-engine/graph_engine/runtime/scheduler.py:599-612`
   - Three ORs (`not prepared`, expired lease, `cancel_requested`) sit immediately above the expired-lease `LeaseUnavailableError` raise. Expired prepared-undispatched still `continue`s so `test_expired_non_adopted_recovery_does_not_conflict_on_resume` stays unweakened. Correct, easy to misread. `_live_activity` is fetched twice.

4. **Stale `tests/phase5/…` evidence paths remain in the Phase 5 acceptance builder.**
   - File: `tests/phase6/conformance.py:641-649`
   - `PHASE5_REPLACEMENT_TEST` moved to `tests/product/…` (`conformance.py:711`). `_criterion_evidence` still cites the pre-rename gate files. Harmless if the builder is only used to authenticate the frozen Task 5 document; misleading if someone regenerates acceptance.

5. **Recovery fixture ledger split and host stand-in (carry-forward).**
   - File: `packages/graph-engine/tests/runtime/test_activity_recovery.py:802-1025`
   - `_persist_prepared_attempt` keeps the pre-close `Ledger` on the fixture and rebinds the host to a new `Ledger` on the reopened root. Disk-backed reads make the no-new-events assertion work; an in-memory cache would hide a dispatch. `_PreparedDispatchHost` is a ~130-line production-host stand-in, justified only because the happy path must reach `succeeded`.

6. **Phase 6 Tasks 17–18 are `verified_complete` while Step 4/5 are pending and required gates were red.**
   - File: `residual-disposition.json` last row; `conformance.py:83`
   - The evidence string names the pending controller steps. `acceptance.md` already says the cutover is not fully accepted. The disposition still tells the admission checker this closeout task is done. Prefer keeping `accepted_with_waivers` as the only success signal for Tasks 17–18.

---

### Recommendations

- Rename the live-runner `deleted CLI alias` identifiers and notes before the next isolated OpenCode invocation. That is the only in-range production/harness alias leak.
- Treat `063450-3e30e331` as a blocked product run. A new live invocation is required if OpenCode live is ever closed. Do not resume that change as `achieved`, and do not edit frozen `phase6-handoff.json` or Change-local `admission.json`.
- Controller Step 4 should review the whole branch, not only `9c86bae..6740dce`. Controller Step 5 should rerun affected plus full gates from a clean committed HEAD. Do not treat the dirty-tree red suite as a new defect in this range; `tests/product/conftest.py` `pytest_plugins` is a rename of the pre-existing `tests/phase5/conftest.py` shape.
- Leave leftover `production_host` / `production_worker` hunks unstaged. They are out of this session’s file set.
- If residual vocabulary is revised, do not weaken `test_expired_non_adopted_recovery_does_not_conflict_on_resume` to make expired prepared-undispatched raise `LeaseUnavailableError`. That skip is required by the unweakened test.

---

### Assessment

**Ready to merge?** No

**Reasoning:** The range delivers a correct `resume_running` seam, a complete product/benchmark rename, current-doc and export locks, and an honest `accepted_with_waivers` evidence pack — not a completed hard cutover. OpenCode live failed closed on an in-scope `invalid_output` and is mislabeled `deferred_out_of_scope`; the final live runner still emits `deleted CLI alias` in evidence notes; Completion Definition items 3, 7, and 8 remain unmet. Merge this only as a waiver snapshot after fixing the two Important items, not as “remaining phases closed.”
