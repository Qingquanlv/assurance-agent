# Codegen locked outputs (case-design style)

Date: 2026-09-15

## Problem

API codegen lets the model choose write paths under directory prefixes
`qa/tests/api/` and `qa/tests/testdata/`. `allowed_outputs` currently lists
those directory roots. `under_write_root` also treats the root itself as
legal, so a model can write extensionless files named `qa/tests/api` and
`qa/tests/testdata`. Durable `qa/tests/api/` is already a directory from
init-test-runtime. Seal then fails as a permanent workspace violation.
Finalize never rejects the path, the 10× invalid-output retry never runs,
and the family graph maps codegen attempt failure to `done`.

Case-design does not have this hole: the host locks exact files
(`qa/cases/<module>/case.yaml` plus sidecars), `allowed_outputs` is that
exact set, and finalize requires the receipt to match.

## Decision

Use case-design’s exact-path lock for all four codegen families. The host
derives one test file and one testdata file per case module per family.
The model may write only those files. Finalize validates; it does not
rewrite paths. Graph topology stays `codegen → codegen-review → done`.

## Path rules

Family directory tokens:

| family | family_dir |
|---|---|
| api | `api` |
| e2e | `e2e` |
| fuzz | `fuzz` |
| performance | `perf` |

A reviewed case file must be an exact `qa/cases/<module>/case.yaml` path
(same shape as intake `case_delta_paths`: at least four POSIX parts, prefix
`qa/cases`, leaf name `case.yaml`).

- `module` = the POSIX path between `qa/cases/` and `/case.yaml`
- `leaf` = the last segment of `module`

Locked generated files for that module and family:

- test: `qa/tests/<family_dir>/<module>/test_<leaf>.py`
- testdata: `qa/tests/testdata/<family_dir>/<module>.py`

Examples:

- `qa/cases/dept/case.yaml` + api →
  `qa/tests/api/dept/test_dept.py` and `qa/tests/testdata/api/dept.py`
- `qa/cases/foo/bar/case.yaml` + e2e →
  `qa/tests/e2e/foo/bar/test_bar.py` and `qa/tests/testdata/e2e/foo/bar.py`

Several modules produce several pairs. Families never share a generated
file. Testdata uses the full `module` path (not only `leaf`) so two modules
that share a last segment cannot collide.

## Scope

`build_codegen_scope` receives the authenticated case file paths together
with the family-filtered cases. When each `qa/cases/<module>/case.yaml` is
loaded, the family-filtered `added` and `modified` `case_id`s in that file
belong to that module. It groups those ids and adds:

- `locked_modules`: one row per module
  `{module, case_ids, test_file, testdata_file}`
- `locked_outputs`: the generated files above plus
  `qa/results/codegen/<family>-codegen-summary.md` and
  `qa/results/codegen/<family>-generated-files.json`

Every `scope.case_id` appears in exactly one `locked_modules` row.
`locked_outputs` is sorted and unique. A case path that is not
`qa/cases/<module>/case.yaml` is `InputError` (not retryable).

`FAMILY_TARGET_ROOTS` (and therefore scope `write_roots`, which must still
equal that table) becomes the family policy prefixes, with testdata
narrowed per family:

- api: `qa/tests/api/`, `qa/tests/testdata/api/`
- e2e: `qa/tests/e2e/`, `qa/tests/testdata/e2e/`
- fuzz: `qa/tests/fuzz/`, `qa/tests/testdata/fuzz/`
- performance: `qa/tests/perf/`, `qa/tests/testdata/perf/`

`write_roots` is not the write allowlist. Prefix match, including an exact
root such as `qa/tests/api`, is not sufficient to accept a generated path.

## Prepare and agent contract

`codegen_outputs` returns `scope.locked_outputs` only. Directory roots such
as `qa/tests/api` and `qa/tests/testdata` must not appear.

Prepare sends that exact list as OpenCode `allowed_outputs` and in the
instruction JSON.

The model:

- writes tests only to each locked `test_file`
- writes helpers only to each locked `testdata_file` (the file is required,
  even if short)
- authors mapping `symbol` per `case_id`

`mapping.entries` must cover `scope.case_ids` exactly once.
`target_file` is required and must equal the host-locked test file for that
case’s module. No coerce: a mismatch is `OutputError`.

`files[].repo_path` must be a member of the locked generated files (test +
testdata). Extra or missing generated files are `OutputError`.

Four codegen skills state the lock: do not invent paths; write only
`locked_outputs`.

## Finalize

Finalize does not rewrite paths. It authenticates workspace bytes and
checks:

1. Receipt paths under `qa/tests/` equal the locked test + testdata set.
2. Required codegen result sidecars are present.
3. Every `mapping.target_file` equals the locked test file for that case.
4. Every `files[].repo_path` is in the locked generated set.
5. Each locked generated path is a regular file in the write root.

Failures 1–5 are `OutputError` (current-node invalid-output retry, up to
10). Host cannot derive a module from a case path: `InputError`.

Seal of a file over a directory should no longer occur for these writes,
because `allowed_outputs` never grants the directory root as a file.

## Out of scope

- Graph topology, `_ATTEMPT_PATHS`, or codegen-review contract
- Host rewriting a bad path into the locked file
- Restoring plan / plan-reviewer nodes
- Changing case-design

## Tests

- Scope derivation for each family from `qa/cases/dept/case.yaml`
- Several modules produce several locked pairs
- Nested module `qa/cases/foo/bar/case.yaml` uses full module in testdata
- Prepare `allowed_outputs` is the exact lock; no directory roots
- Finalize accepts a complete lock set with host-matching `target_file`
- Finalize `OutputError`: write-root path, missing testdata, extra file,
  `target_file` mismatch
- Finalize `InputError`: case path is not `qa/cases/<module>/case.yaml`
- Existing codegen/scope tests that assume directory-root
  `allowed_outputs` or model-chosen `target_file` follow the lock
