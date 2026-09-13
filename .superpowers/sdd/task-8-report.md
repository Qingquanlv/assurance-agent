# Task 8 Report: Version bump to 0.3.0

## Status

DONE_WITH_CONCERNS

## TDD Evidence

### RED (Step 2)

Flipped `tests/product/test_product_providers.py` first: six-wheel catalog versions and capability `version_specifier` to `==0.3.0`. Production still advertised `0.2.0`.

Command:

```bash
uv run pytest tests/product/test_product_providers.py -v
```

Output:

```
FAILED test_source_catalog_is_six_wheels_plus_opencode
  version='0.2.0' != version='0.3.0'

FAILED test_provider_returns_one_minimal_opencode_manifest
  {'assurance.execution': '==0.2.0', ...} != {'assurance.execution': '==0.3.0', ...}

2 failed, 3 passed
```

Exit code: 1

The brief `-k "plugin or 0.2.0 or version_specifier"` filter does not select these pin tests (names lack those tokens) and was green before the bump. Failure reason was stale `0.2.0` pins, not a typo.

### GREEN (Step 4)

Command:

```bash
uv run pytest tests/product -k "plugin or 0.2.0 or version_specifier" -v
uv run pytest tests/product/test_product_providers.py \
  tests/product/test_project_configuration.py \
  tests/product/test_cli_compile.py \
  tests/product/test_product_composition.py \
  tests/product/test_binding_builder.py \
  tests/product/test_product_packaging.py \
  tests/product/test_project_configuration_security.py -v
uv run pytest packages/capabilities/*/tests/test_plugin.py -v
```

Output:

```
19 passed, 850 deselected   # -k plugin filter
62 passed, 1 failed         # composition set (see concerns)
94 passed                   # six plugin tests + pin + config security
```

Pin / compile / packaging / configuration / capability plugin tests are green.

## What changed

- Six capability wheels and `assurance-product` are `0.3.0`. Inter-wheel and product capability pins are `==0.3.0`.
- `graph-engine` stays `0.2.0` (product dependency and binding-wheel METADATA).
- Regenerated `product-declaration-opencode.json` and capability `plugin-declaration.json` files.
- Updated project-config and phase4 fixtures that pinned the six plugins at `==0.2.0`.
- `docs/usage.md` §8 now says `==0.3.0` (file is gitignored under `docs/`).
- `uv.lock` records the new workspace versions.

## Concerns

- Full `tests/product -v` was not run (869 tests; earlier tasks still had leftover failures).
- `test_unselected_adapter_source_is_rejected_before_provider_import` still fails with `wheel declaration path is absent from the authenticated snapshot` instead of `runtime.cursor|agent-runtime-cursor`. Looks pre-existing, not pin-related.
- Historical `benchmark/assurance-product/results/**/plugin.yaml` and `graph-engine` boot fixture still say `==0.2.0`.
- `docs/usage.md` update cannot be committed (`docs/` is gitignored).
