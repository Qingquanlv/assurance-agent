# Task 1 Report: 冻结 User 业务断言与来源

## Status

DONE

## Implementation

- Added closed, frozen intake contracts for `BusinessAssertionV1`, discriminated literal/input expected values, `AssertionSourceV1`, and `AssertionSourcesV1`.
- Added `validate_assertion_provenance` to reject empty assertion sets, duplicate assertion IDs, case revision/spec digest drift, and assertions whose `source_id` has no authoritative sidecar source.
- Restricted authoritative origins to authenticated `requirement` or `reviewed_input` content. `reviewed_input` additionally requires an authenticated review ref. `source_code` and a bare `decision=accepted` are rejected.
- Deep-froze nested literal JSON values while preserving their JSON schema and canonical serialization.
- Added the `TC_USER_CREATE_001` fixture with the exact eight assertion IDs, subjects, and expected forms from the brief. Inputs freeze `is_active=true`, `is_superuser=false`, and `dept_id=null`; there is no `execution_id`.
- Registered `assertion-sources.v1.schema.json` in the intake wheel and static plugin declaration, and exported the new contracts through `assurance_intake.contracts`.
- Added explicit `assertion_source_paths` to case-design/review/finalize contracts. Paths must map one-for-one to locked case directories, are included in allowed outputs and review inputs, and are parsed by the deterministic finalizer as typed assertions plus source sidecars. Inputs without those paths retain legacy authoring behavior.
- Versioned `ResolvedAssurancePlan` compatibly: legacy inputs still seal/read as schema version `1` with the legacy projection; explicit authenticated verification policy material produces schema version `2` and binds `validation_profile`, policy resource ID, and policy digest into `plan_digest`. SUT and technical configuration identities remain outside the root plan.
- Authenticated `.aa/verification-policy.yaml` only for explicit new-profile inputs; legacy plan resolution/loading does not require it.
- Updated case-design/reviewer prompts and skills so the formal sidecar is the only allowed provenance metadata channel and product source cannot authorize business expected values.

## TDD RED and GREEN Evidence

### 1. Empty assertion contract

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
E   ModuleNotFoundError: No module named 'assurance_intake.contracts.verification'
1 error in 0.46s
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
....                                                                     [100%]
4 passed in 0.36s
```

### 2. Authoritative source admission

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
E   ImportError: cannot import name 'AssertionSourcesV1'
1 error in 0.51s
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
......                                                                   [100%]
6 passed in 1.30s
```

### 3. Fixture provenance closure

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
E   ImportError: cannot import name 'validate_assertion_provenance'
1 error in 0.51s
```

After the minimal validator, the first GREEN attempt exposed an overly strict generic ID validator against the required `api.http_status` ID:

```text
6 failed, 6 passed in 0.48s
E   Value error, assertion and source IDs must be qualified
```

Final GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
............                                                             [100%]
12 passed in 0.43s
```

### 4. Public schema and plugin registration

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
E   ImportError: cannot import name 'AssertionSourcesV1' from 'assurance_intake.contracts'
1 error in 0.44s
```

Intermediate RED after public export:

```text
FAILED ... test_verification_contracts_are_public_and_the_source_schema_is_registered
E   FileNotFoundError: .../schemas/assertion-sources.v1.schema.json
1 failed, 12 passed in 0.38s
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
.............                                                            [100%]
13 passed in 0.36s
```

### 5. Root plan v1/v2 identity

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py -q
E   ImportError: cannot import name 'BusinessVerificationPolicyV1'
1 error in 0.46s
```

The first implementation then correctly exposed a normalized-projection digest bug:

```text
5 failed, 5 passed in 0.42s
E   Value error, plan_digest does not match the plan projection
```

GREEN after sealing the normalized version-aware model projection:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py -q
..........                                                               [100%]
10 passed in 0.36s
```

### 6. Explicit sidecar routing

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
E   assertion_source_paths
E     Extra inputs are not permitted
1 failed, 14 passed in 0.41s
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py -q
...............                                                          [100%]
15 passed in 0.37s
```

### 7. Authenticated verification policy bytes

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_prepared_quality_goals.py -q -k authenticates
..FF                                                                     [100%]
2 failed, 2 passed, 5 deselected in 0.89s
E   Failed: DID NOT RAISE ValueError
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_prepared_quality_goals.py -q -k authenticates
....                                                                     [100%]
4 passed, 5 deselected in 0.88s
```

### 8. Deep-frozen literal expected JSON

RED:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_literal_expected_nested_json_cannot_be_mutated_after_admission -q
F                                                                        [100%]
E   Failed: DID NOT RAISE TypeError
1 failed in 0.39s
```

GREEN:

```text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_literal_expected_nested_json_cannot_be_mutated_after_admission -q
.                                                                        [100%]
1 passed in 0.40s
```

## Final Verification

```text
$ uv run ruff format --check packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/verification_support.py
50 files already formatted

$ uv run ruff check packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/verification_support.py
All checks passed!

$ uv run pyright packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/verification_support.py
0 errors, 0 warnings, 0 informations

$ uv run pytest packages/capabilities/assurance-intake/tests -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 81%]
..................................................                       [100%]
266 passed in 2.48s

$ uv run pytest tests/product/test_acg_plan_loading.py tests/product/test_acg_plan_recovery.py tests/product/test_agent_execution_contracts.py tests/product/test_graph_revision_contracts.py -q
..............................                                           [100%]
30 passed in 15.41s
```

## Files Changed

- New contracts/schema/tests/fixtures: `assurance_intake/contracts/verification.py`, `resources/schemas/assertion-sources.v1.schema.json`, `test_verification_contracts.py`, `tests/verification_support.py`, and `tests/fixtures/verification/{user-case,user-sources}.json`.
- Contract and plan wiring: `contracts/{__init__,agent,plan}.py`, `graphs/{nodes,state}.py`, `operations/{agent_skills,finalize,plan_artifacts,resolve_plan}.py`.
- Published resources: `plugin.py`, `plugin-declaration.json`, case-design/review prompts and skills.
- Existing contract tests: `test_contracts.py`, `test_prepared_quality_goals.py`, and `test_resolve_plan.py`.

## Self-review

- Standards: no documented AGENTS.md boundary violation found. Python wheels own all new contracts/handlers; `.aa/verification-policy.yaml` remains closed organization data and is never scanned for executable behavior. No new Agent, product node, machine plan, runtime profile switch, or `execution_id` was added.
- Spec: all eight assertion IDs/subjects/expected forms match the Task 1 brief. Source-code-only and bare accepted decisions fail admission. Legacy case inputs and schema-v1 plan bytes remain readable; new guarantees require explicit sidecar/policy material.
- Mutation review: removing origin closure, content/review refs, assertion/source uniqueness, revision/digest checks, sidecar path closure, policy byte authentication, or deep JSON freezing causes a named test to fail.
- Scope review: `user-plan.json` remains absent intentionally because Task 2 owns the machine plan fixture; the shared helper reserves its approved name for that task.

## Concerns

None.

---

## Review Fix Round

### Implementation

- Bound every accepted source to independently authenticated preparation refs.
  A requirement source must match the one frozen current-change requirement.md
  ref exactly; a reviewed_input source must bind both its content and review ref
  to admitted requirement/review evidence.
- Derived sidecar revision from the frozen v2 verification profile and
  spec_digest from the frozen plan requirement_digest. Finalize no longer uses
  the sidecar's own values as their expected values.
- Re-authenticated authority bytes during design finalize and authenticated
  case, matrix, and sidecar bytes during review finalize. Review prepare now
  parses cases and sidecars through the same typed provenance closure.
- Required exactly one locked sidecar path per locked case path for v2 plans in
  both design and review prepare/finalize. Legacy v1 plans still admit legacy
  authoring without sidecars.
- Preserved authority refs through the validation-repair selector.
- Kept resolved-assurance-plan.v1.schema.json unchanged and added a separately
  registered resolved-assurance-plan.v2.schema.json. Version projection tests
  enforce semantic model/schema consistency for both versions.

### TDD RED Evidence

Independent source authority was missing:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_assertion_source_ref_must_match_an_independently_admitted_requirement -q
F                                                                        [100%]
E   TypeError: validate_assertion_provenance() got an unexpected keyword argument 'requirement_ref'
1 failed in 0.41s
~~~

Published plan version models/schema were missing:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_assertion_source_ref_must_match_an_independently_admitted_requirement packages/capabilities/assurance-intake/tests/test_resolve_plan.py::test_published_plan_schemas_match_each_version_and_validate_both_profiles -q
ERROR: found no collectors for .../test_resolve_plan.py::test_published_plan_schemas_match_each_version_and_validate_both_profiles
E   ImportError: cannot import name 'ResolvedAssurancePlanV1' from 'assurance_intake.contracts.plan'
1 error in 0.47s
~~~

New-profile design and review could omit sidecars:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_verified_case_design_prepare_requires_one_source_sidecar_per_case packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_verified_case_review_prepare_requires_one_source_sidecar_per_case -q
FF                                                                       [100%]
E   AssertionError: assert 'succeeded' == 'failed'
E   AssertionError: assert 'succeeded' == 'failed'
2 failed in 0.92s
~~~

Review prepare authenticated bytes but did not parse typed provenance:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_verified_case_review_prepare_rejects_invalid_typed_sidecar -q
F                                                                        [100%]
E   AssertionError: assert 'succeeded' == 'failed'
1 failed in 0.89s
~~~

The validation-repair selector dropped frozen authority refs:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_verified_case_design_repair_preserves_frozen_authority_refs -q
F                                                                        [100%]
E   AssertionError: assert [] == [{'path': 'qa/changes/CH-1/requirement.md', 'digest': 'cccc...'}]
1 failed in 0.41s
~~~

### TDD GREEN Evidence

The source authority, schema, and v2 prepare slices first turned green together:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py packages/capabilities/assurance-intake/tests/test_resolve_plan.py packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_verified_case_design_prepare_requires_one_source_sidecar_per_case packages/capabilities/assurance-intake/tests/test_agent_skills.py::test_verified_case_review_prepare_requires_one_source_sidecar_per_case -q
..............................                                           [100%]
30 passed in 0.87s
~~~

Real design/review prepare/finalize mutation coverage then passed:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py -q -k 'verified_case_design_prepare_requires_one_source_sidecar_per_case or verified_case_review_prepare_requires_one_source_sidecar_per_case or verified_case_design_finalize_rejects_sidecar_authority_mutation_after_prepare or verified_case_review_finalize_rejects_sidecar_changed_after_prepare or verified_case_review_prepare_rejects_invalid_typed_sidecar'
.........                                                                [100%]
9 passed, 77 deselected in 0.84s
~~~

Repair propagation passed:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_verification_contracts.py::test_verified_case_design_repair_preserves_frozen_authority_refs -q
.                                                                        [100%]
1 passed in 0.37s
~~~

The final mutation matrix covers post-prepare authority bytes, source-ref
digest, revision, spec digest, origin, decision, and review-sidecar byte drift:

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_agent_skills.py -q -k 'verified_case_design_finalize_rejects_sidecar_authority_mutation_after_prepare or verified_case_review_finalize_rejects_sidecar_changed_after_prepare or verified_case_review_prepare_rejects_invalid_typed_sidecar'
........                                                                 [100%]
8 passed, 79 deselected in 1.02s
~~~

### Final Verification

~~~text
$ uv run ruff format --check packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/acg_plan_fixture.py
50 files already formatted

$ uv run ruff check packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/acg_plan_fixture.py
All checks passed!

$ uv run pyright packages/capabilities/assurance-intake/assurance_intake packages/capabilities/assurance-intake/tests tests/acg_plan_fixture.py
0 errors, 0 warnings, 0 informations

$ uv run pytest packages/capabilities/assurance-intake/tests -q
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
...............................................................          [100%]
279 passed in 2.59s

$ uv run pytest tests/product/test_acg_plan_loading.py tests/product/test_acg_plan_recovery.py tests/product/test_agent_execution_contracts.py tests/product/test_graph_revision_contracts.py -q
..............................                                           [100%]
30 passed in 15.77s
~~~

### Files Changed in Fix Round

- Provenance and version contracts: contracts/verification.py and
  contracts/plan.py.
- Prepare/finalize and selection: operations/agent_skills.py,
  operations/finalize.py, and graphs/nodes.py.
- Published schema registration:
  resources/schemas/resolved-assurance-plan.v2.schema.json, plugin.py, and
  plugin-declaration.json.
- Coverage: test_verification_contracts.py, test_resolve_plan.py,
  test_agent_skills.py, test_contracts.py, and tests/acg_plan_fixture.py.

### Fix-round Self-review

- The sidecar cannot select its own expected revision or spec digest, and an
  Agent-authored decision=accepted has no authority without matching frozen
  refs and authenticated bytes.
- Review prepare and finalize both parse typed assertions and source sidecars;
  review finalize also rejects sidecar byte changes after prepare.
- V2 omission fails at prepare and finalize. V1 empty-sidecar behavior remains
  unchanged.
- The old v1 schema file and ID were not rewritten; v2 has its own file and ID.
- No Task 2 machine plan, runtime switching, SUT identity, or execution ID was
  introduced.

### Fix-round Concerns

None.

---

## Review Fix Round 2

### Implementation

- Made schema_version a required field on the published
  ResolvedAssurancePlanV2 projection instead of defaulting it to version 2.
- Regenerated only resolved-assurance-plan.v2.schema.json and updated its
  canonical schema lock digest.
- Preserved the runtime ResolvedAssurancePlan legacy default of version 1 and
  left resolved-assurance-plan.v1.schema.json unchanged.
- Added a regression that removes schema_version from a valid v2 plan and
  proves rejection at the published-schema requirement, v2 projection model,
  and runtime model/admission boundaries.

### TDD RED Evidence

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py::test_v2_plan_schema_and_models_reject_an_implicit_schema_version -q
F                                                                        [100%]
E       AssertionError: assert 'schema_version' in ['change_id', 'requirement_digest', 'candidate_test_families', 'proposed_test_families', 'selected_test_families', 'quality_goal', ...]
1 failed in 0.42s
~~~

### TDD GREEN Evidence

~~~text
$ uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py::test_v2_plan_schema_and_models_reject_an_implicit_schema_version packages/capabilities/assurance-intake/tests/test_resolve_plan.py::test_published_plan_schemas_match_each_version_and_validate_both_profiles packages/capabilities/assurance-intake/tests/test_contracts.py::test_intake_product_lock_schema_mapping_is_current_only -q
...                                                                      [100%]
3 passed in 0.40s
~~~

### Final Verification

~~~text
$ uv run ruff format --check packages/capabilities/assurance-intake/assurance_intake/contracts/plan.py packages/capabilities/assurance-intake/tests/test_resolve_plan.py packages/capabilities/assurance-intake/tests/test_contracts.py
3 files already formatted

$ uv run ruff check packages/capabilities/assurance-intake/assurance_intake/contracts/plan.py packages/capabilities/assurance-intake/tests/test_resolve_plan.py packages/capabilities/assurance-intake/tests/test_contracts.py
All checks passed!

$ uv run pyright packages/capabilities/assurance-intake/assurance_intake/contracts/plan.py packages/capabilities/assurance-intake/tests/test_resolve_plan.py packages/capabilities/assurance-intake/tests/test_contracts.py
0 errors, 0 warnings, 0 informations

$ uv run pytest packages/capabilities/assurance-intake/tests/test_resolve_plan.py packages/capabilities/assurance-intake/tests/test_contracts.py packages/capabilities/assurance-intake/tests/test_resources.py -q
....................................................                     [100%]
52 passed in 0.55s

$ uv run pytest packages/capabilities/assurance-intake/tests -q
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
................................................................         [100%]
280 passed in 2.57s
~~~

### Files Changed in Review Fix Round 2

- assurance_intake/contracts/plan.py
- assurance_intake/resources/schemas/resolved-assurance-plan.v2.schema.json
- assurance_intake/tests/test_resolve_plan.py
- assurance_intake/tests/test_contracts.py
- this report

### Self-review and Concerns

The v2 schema and both model boundaries now require explicit version identity.
The v1 model, schema resource, and legacy runtime default are unchanged.

Concerns: None.
