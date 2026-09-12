# Task 6 Report: Stop the benchmark host seed copy

## Status

DONE

## TDD Evidence

The brief is implement-then-verify (delete the host seed path, replace the four phase5 seed-copy tests, then the listed pytest). No separate RED flip was required.

### GREEN (Step 3)

Command:

```bash
uv run pytest tests/product/test_phase5_benchmark_change_layout.py -v
```

Output:

```
============================== 21 passed in 0.65s ==============================
```

Exit code: 0

`test_run_item_does_not_materialize_test_runtime_seed` passed. The four host seed-copy tests are gone. Fixture `benchmark/assurance-product/fixtures/vue-fastapi-admin-tests-runtime-v1` remains on disk.

## What changed

- `run_item.py` no longer copies `vue-fastapi-admin-tests-runtime-v1` and no longer import-checks seed symbols before `aa run`.
- Deleted `_TEST_RUNTIME_SCHEMA`, `_TEST_RUNTIME_MANIFEST_SHA256`, `_test_runtime_seed_root`, `_dept_runtime_symbols`, `_materialize_test_runtime_seed`, `_VALIDATE_TEST_RUNTIME`, `_validate_test_runtime_symbols`.
- Dropped `seed_receipt` from `_SutRuntime`.
- When writing evidence, `evidence["test_runtime_seed"]` is `{path, digest}` of sealed `qa/results/init/test-runtime.json` if that path is a regular file; otherwise `None`. No invented digest.
- Replaced the four phase5 seed-copy tests with `test_run_item_does_not_materialize_test_runtime_seed`. Updated `_SutRuntime` fakes that passed `seed_receipt`.

## Files Changed

| File | Action |
|------|--------|
| `benchmark/assurance-product/run_item.py` | Remove host seed copy; evidence reads Kernel receipt |
| `tests/product/test_phase5_benchmark_change_layout.py` | Replace four seed-copy tests; drop `seed_receipt` fakes |

## Commit

none

## Concerns

none
