# Shipping pilot implementation plan

Goal: extend the existing Vue/FastAPI administration app with shipping orders and a real, independently persisted carrier service; swap only the carrier URL for HTTP Mock acceptance.

Approved design: one shipping list, create modal and detail drawer; create a draft, request a waybill, reconcile uncertain outcomes. Preserve all existing User behavior. No payment, inventory, callback or production deployment in this iteration.

Architecture: the SUT owns shipping orders and operation history. The carrier owns waybills in its own SQLite database. A stable order reference is the carrier idempotency key. HTTP timeouts, 5xx and malformed responses leave the order UNKNOWN; validated business rejection is REJECTED; validated success is ACCEPTED. Database leases serialize operations and expire after process crashes. Each external operation has a deadline shorter than its lease. Only known successful records can become ACCEPTED.

Files: SUT app/models/shipping.py, app/schemas/shipping.py, app/controllers/shipping.py, app/api/v1/shipping.py, app/core/shipping_seed.py; register models/routes/settings/startup. Frontend web/src/views/shipping/index.vue, web/src/api/index.js and development proxy. Pilot logistics_service.py, run.py, prepare.py, shipping.patch, test_shipping.py and README.md.

Review focus: duplicate concurrent submissions; commit-before-timeout recovery; malformed/mismatched remote identity; process restart with a pending operation; existing database menu/API grants without privilege escalation.

- [x] 1. Write real HTTP tests for draft creation, carrier idempotency, live submission and reconciliation; observe missing route failure. Implement provider and SUT endpoints. Verify both independent databases and old authentication.
- [x] 2. Add fault-server tests for retry, business rejection, timeout after acceptance, unavailable service, response corruption and duplicate requests. Implement bounded retries and persistent state transitions. Verify no extra waybills and no false success.
- [x] 3. Integrate a Vue/Naive UI page using existing components and permissions. Seed menu/API grants idempotently for new and existing databases. Build and exercise the browser from creation to a real waybill.
- [x] 4. Supply isolated launcher and reproducible baseline patch. Test launcher cleanup/restart and patch replay. Run repository checks, independent code review and document evidence.

Execution: inline in the current authorized worktree. No commits or external publication are part of this request. SUT source remains ignored by the parent repo, so the patch is the reviewable source of its modifications.

Evidence: missing route acceptance failed with 404 before implementation. Final pilot suite: 21 passed. Repository suite: 4842 passed, 18 skipped. Independent review found corrupt gzip handling and pagination-reset issues; fixed with RED→GREEN HTTP regression and browser page-two polling verification respectively. Launcher TIME_WAIT restart failure reproduced and fixed with a dedicated regression. Final patch replay matched all 13 SUT files byte-for-byte. No deferred review findings.
