# Task 6 implementation report

## RED evidence

- Command: `uv run pytest packages/capabilities/assurance-generation/tests/test_verified_codegen.py packages/capabilities/assurance-execution/tests/test_verified_execution.py -q`
- Result: exit 2 during collection. `test_verified_execution.py` could not import `VerifiedExecutionResultV1` from `assurance_execution.contracts.verification`. This is the intended first failure: the verified host/facade has no distinct typed dispatch result or cycle contract yet.
- Final structural review added duplicate-symbol and executable return-annotation bridge cases. The focused generation test then failed 2 cases (9 passed), proving the validator still admitted code beyond the fixed bridge shape; after closing those AST fields it passed 11/11.

## Implementation decisions

- Verified generation is an authenticated closure over the validation profile, ReviewedCase, coverage epoch, root assurance plan, independent case-execution plan, and every mapped CaseSpec digest. Agent output remains a candidate; finalize, fixer, generated-files validation, and cycle publication re-read committed bytes and reject stale or foreign identities.
- Verified API tests are limited to the installed Task 4 bridge shape: one parameter-free test function per mapped case whose only statement calls `execute_case(<case_id>)`. Direct HTTP, SQLite, Trace, expected-value, credential, execution-id, verdict, and extra-assert logic is rejected structurally.
- `ExecutionDispatchResultV1` carries either the legacy execution evidence or the typed host-authenticated `VerifiedExecutionResultV1`. Verified profiles bypass the legacy Agent result and evidence-normalization path.
- `VerifiedExecutionCycleResultV1` is separate from legacy PASS/FAIL output. It preserves profile, accepted generation identities, execution/attempt/batch and mapping identities, current raw evidence and index, completion (`collected` or `incomplete`), and the kernel receipt. The Task 4 host facade writes immutable per-execution evidence and atomically replaces only the current pointer for later attempts.
- Task 5 already installed semantic `execution.execute` and `execution.run` contracts. Some execution graph tests still constructed the older internal Agent contract; those fixtures were corrected without changing the installed semantic IDs or the 34 Agent bindings.

## GREEN evidence

- Required focused command from the brief: 171 passed in 49.59s.
- Focused product graph/profile/recovery/semantic suites: 47 passed in 93.84s.
- Generation and execution package suites excluding the environment-only real OCI qualification: 742 passed in 82.38s.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 808 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.
- `uv run lint-imports`: 13 contracts kept, 0 broken.
- `git diff --check`: passed.

## Environment limits and review

- The real OCI qualification remains unavailable because Docker and `dist/verification-runner/qualification.json` are absent. No OCI result was synthesized or claimed.
- Real OpenTelemetry qualification is unavailable in this environment and was not claimed.
- The final diff was inspected for generated execution identity, secrets, schema/declaration drift, and Task 7 logic. Execution IDs remain host-owned inputs; no credentials or Task 7 verdict logic were added.
- Running package contract tests and product extracted-composition tests in one combined pytest process exposes an existing module-reload/Pydantic class-identity pollution issue. The brief's required suite and the affected product suites pass in their intended separate process boundaries; no compatibility path was added to hide the pollution.

## Review fix round 1

### RED evidence

- Duplicate bridge identity: the new ordinary-finalize, fixer-finalize, and committed-file tests all failed. Both finalize paths accepted two different Case IDs mapped to the same `(target_file, symbol)`, and the committed validator returned accepted.
- Product composition: `uv run pytest -v tests/product/test_feature_graph_bundles.py` initially produced 6 failures and 3 passes because the test composition installed only the two legacy execution Agent contracts; graph construction correctly required the Task facade contracts.
- Verified side-effect boundary: the first direct missing-`generation_result` test reached both the Task facade host-call builder and its production host (`["host-call", "host"]`). This proved that verified dispatch had no accepted-generation authentication boundary.

### Changes and GREEN evidence

- `CodegenMapping` now rejects duplicate `(target_file, symbol)` identities in addition to duplicate Case IDs, before either dictionary bridge is built. Ordinary finalize, fixer finalize, and committed-file regression tests pass.
- The product feature-bundle test composition now installs execution `TASK_ATTEMPT_CONTRACTS` beside `AGENT_JOB_CONTRACTS`. The graph continues to bind Task facade contracts, and the installed Agent binding inventory remains exactly 34. The file passes standalone: 9 passed.
- Verified Task dispatch now requires `generation_result` for `api_db.v1` and `api_db_trace.v1`, then authenticates the ReviewedCase/change/epoch closure, root plan, independent machine plan, committed source and mapping closure, CaseSpec identities, profile, and mapped node against frozen execution input. Legacy profile `None` retains its optional generation input.
- Ten direct negative categories cover missing generation, stale epoch, ReviewedCase drift, root-plan drift, machine-plan drift, mapping drift, generated-source drift, CaseSpec drift, profile drift, and old-epoch replay. Every case invokes both the actual `ProfiledExecutionExecutor` Task facade and actual `VerifiedAttemptHandler`; facade `_call`/host counts remain zero, HTTP count remains zero, and no `action_started.json` is written. Together with the valid closure case: 11 passed.
- Recovery fixtures now install a real accepted generation closure instead of bypassing authentication. The five production host cut/reconcile stages pass standalone.
- Final focused regression across verified execution, product graph/recovery, codegen, and committed validation: 170 passed in 55.94s. Generation's broader verified/codegen/validator run: 119 passed. Task 6 broader focused run before the shared fixture cleanup: 221 passed in 48.75s.
- Final static checks after implementation: Ruff passed; Ruff format reported 808 files already formatted; Pyright reported 0 errors, 0 warnings, 0 informations; lint-imports kept all 13 contracts with 0 broken; `git diff --check` passed.

### Full-suite qualification

- The first `uv run pytest -v` collected 4,543 tests and reached 30%. It exposed five recovery-fixture failures at 29%, caused by the fixture's obsolete generation-authentication bypass interacting with installed-wheel module replacement. The bypass was removed and replaced with a real accepted generation closure; the file then passed 5/5.
- A clean standard full-suite rerun was started after that fix; final result is recorded below before commit.

### Isolation correction and final gates

- The first complete run finished with 4,481 passed, 23 skipped, 38 failed, and 1 error. The 38 failures were all caused by extracted-wheel tests purging and re-importing workspace package modules without restoring the original module objects. That left later tests holding Pydantic classes from different module instances. The test composition harness now activates extracted modules only for each product test module and restores the exact original workspace module objects afterward; no production class-identity compatibility path was added.
- The pollution source plus execution recovery reproducer passed 13 tests in one process after the restoration fix. A broader product-to-package sequence covering plan loading, feature graph bundles, verified recovery/profile, verified execution, codegen, and generated-file validation passed 191 tests in 139.09s.
- The clean standard `uv run pytest -v` rerun finished with 4,519 passed, 23 skipped, and the single expected environment-only error in 1,101.60s. The error is `test_real_oci_isolation_and_parent_http_sqlite`: `dist/verification-runner/qualification.json` is absent, so the real OCI runner cannot preflight. No qualification artifact or Docker result was fabricated.
- Final post-isolation static gates: `uv run ruff check .` passed; `uv run ruff format --check .` reported 809 files already formatted; `uv run pyright` reported 0 errors, 0 warnings, 0 informations; and `uv run lint-imports` kept all 13 contracts with 0 broken.
