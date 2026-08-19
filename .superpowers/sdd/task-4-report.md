# Task 4 Report: Physical kernel vs product resource split

## Status

DONE_WITH_CONCERNS

## What you implemented

Physically split kernel YAML/JSON/rules/OpenCode plugins out of `assurance_agent/_resources/` into `assurance_agent/_resources_kernel/`. Did not create `packages/assurance-kernel/` (Task 5). Did not change `_PRODUCT_PREFIXES`.

- `git mv` kernel files into `_resources_kernel/{schemas,rules,opencode}`:
  - `policy-default.yaml`, `ingest-artifact-catalog.yaml`, `explore-advisory.schema.json`, `explore-context.schema.json`
  - `failure-classification.yaml`
  - `opencode/plugins/`, `opencode/tools/`, `opencode/INSTALL.md`
- Left in product `_resources/`: `workflow-schema.yaml`, `execution-contracts.yaml`, `skills/`, `opencode/agents/`
- `assurance_agent/resources.py`: kernel root is `files("assurance_agent") / "_resources_kernel"`; product prefixes resolve to `_product_root` when set, otherwise packaged `_resources`
- `ingest_catalog.py`: dropped `Path(__file__).parents[2]` arithmetic; loads via `resources.read_text("schemas", "ingest-artifact-catalog.yaml")`

Commit `8f4d13d` staged only the listed paths. Unrelated dirty skill / agent markdown under `_resources/` was not committed.

## What you tested and test results

TDD: created `tests/unit/resources/test_resource_roots.py` with the brief’s two tests (file did not exist from Tier 2).

- RED: `uv run pytest tests/unit/resources/test_resource_roots.py -v` → **2 failed** (`policy-default.yaml` still under product `_resources/`; ingest catalog still used `__file__` arithmetic).
- GREEN (brief Step 4): `uv run pytest tests/unit/resources/test_resource_roots.py tests/unit/workflow/graph/test_ingest_catalog.py tests/unit/test_product.py tests/integration/test_cli_workflow_compile.py -q` → **37 passed**.
- `uv run ruff check` / `ruff format --check` on Task 4 Python files → clean.

## TDD Evidence

### RED command

```text
uv run pytest tests/unit/resources/test_resource_roots.py -v
```

Failing output (policy still in product tree; ingest catalog still used `__file__`):

```text
tests/unit/resources/test_resource_roots.py::test_kernel_policy_is_not_under_product_resources FAILED
tests/unit/resources/test_resource_roots.py::test_ingest_catalog_does_not_use_file_arithmetic FAILED

______________ test_kernel_policy_is_not_under_product_resources _______________
    def test_kernel_policy_is_not_under_product_resources() -> None:
        product = Path("assurance_agent/_resources/schemas/policy-default.yaml")
>       assert not product.is_file()
E       AssertionError: assert not True

_______________ test_ingest_catalog_does_not_use_file_arithmetic _______________
    def test_ingest_catalog_does_not_use_file_arithmetic() -> None:
        source = Path("assurance_agent/workflow/graph/ingest_catalog.py").read_text(encoding="utf-8")
>       assert "_resources/schemas/ingest-artifact-catalog.yaml" not in source
E       assert '_resources/...catalog.yaml' not in '"""IngestAr...rn catalog\n'
=========================== short test summary info ============================
FAILED tests/unit/resources/test_resource_roots.py::test_kernel_policy_is_not_under_product_resources
FAILED tests/unit/resources/test_resource_roots.py::test_ingest_catalog_does_not_use_file_arithmetic
========================= 2 failed, 1 warning in 0.04s =========================
```

### GREEN command

```text
uv run pytest tests/unit/resources/test_resource_roots.py tests/unit/workflow/graph/test_ingest_catalog.py tests/unit/test_product.py tests/integration/test_cli_workflow_compile.py -q
```

Passing output:

```text
.....................................                                    [100%]
37 passed, 1 warning in 1.08s
```

## Self-review

- Product prefixes unchanged from Tier 2 (`workflow-schema.yaml`, `execution-contracts.yaml`, `skills/`, `opencode/agents` only). Plugins/tools are kernel.
- `aa init` still syncs `opencode/{agents,tools,plugins}` via `resources.exists` / `_sync`; agents come from the product tree, tools/plugins from the kernel tree.
- `_root_for(("schemas",))` is kernel-only (prefix match is file-specific). `iter_children("schemas")` no longer lists product YAML.
- Did not add `_resources_kernel/**` to `pyproject.toml` hatch `artifacts`; hatchling still includes non-gitignored package data by default.

## Concerns

1. `tests/unit/test_opencode_plugin_boundary.py` hardcodes `assurance_agent/_resources/opencode/plugins/aa.mjs`. That suite fails after the move (ERR_MODULE_NOT_FOUND). Not in this task’s file list.
2. `tests/unit/test_resources.py::test_iter_children_lists_schema_files` expects `workflow-schema.yaml` and `explore-advisory.schema.json` in one `iter_children("schemas")` listing. After the split that listing is kernel-only. Not in this task’s file list.

## Commit

`8f4d13d` Split kernel resource files out of the product _resources tree.

## Review findings (follow-up)

Fixed both Important findings. Did not stage unrelated dirty skill markdown.

### Command

```text
uv run pytest tests/unit/test_opencode_plugin_boundary.py tests/unit/test_resources.py tests/unit/resources/test_resource_roots.py -q
```

### Output

```text
........................................................................ [ 66%]
....................................                                     [100%]
=============================== warnings summary ===============================
assurance_agent/workflow/graph/models.py:70
  /Users/lvqingquan/agent/assurance-agent/assurance_agent/workflow/graph/models.py:70: UserWarning: Field name "schema" in "CompiledWorkflow" shadows an attribute in parent "BaseModel"
    class CompiledWorkflow(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
108 passed, 1 warning in 8.45s
```

### What changed

- `_PLUGIN` now points at `assurance_agent/_resources_kernel/opencode/plugins/aa.mjs`. The bootstrap test copies that plugin into a project-shaped temp tree (`opencode/plugins` + `skills/`) because the plugin still resolves `../../skills` for the `aa init` layout.
- `test_iter_children_lists_schema_files` treats `iter_children("schemas")` as kernel-only (`explore-advisory.schema.json` in, `workflow-schema.yaml` not listed) and reads the product schema via `exists` / `read_text`.
