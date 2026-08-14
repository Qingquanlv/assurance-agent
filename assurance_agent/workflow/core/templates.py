"""Project scaffolding templates written by `aa init`.

Ported from the TS templates (config-yaml.ts, execution-policy.ts,
module-map-yaml.ts, data-knowledge-yaml.ts); project config dir is `.aa`.
"""

from typing import Literal

from pydantic import BaseModel


class InitAnswers(BaseModel):
    api_framework: Literal["pytest", "none"] = "pytest"
    e2e_framework: Literal["playwright", "none"] = "playwright"
    enable_mcp: bool = False
    frontend_path: str | None = None
    backend_path: str | None = None


def build_config_yaml(answers: InitAnswers) -> str:
    api_enabled = answers.api_framework != "none"
    e2e_enabled = answers.e2e_framework != "none"
    frontend = answers.frontend_path or "./frontend"
    backend = answers.backend_path or "./backend"

    return f"""version: 1

project:
  name: ""
  root: .
  layout: default

sources:
  frontend: {frontend}
  backend: {backend}

qa:
  cases: ./qa/cases
  changes: ./qa/changes
  archive: ./qa/archive

tests:
  root: ./tests
  api: ./tests/api
  e2e: ./tests/e2e
  fuzz: ./tests/fuzz
  fixtures: ./tests/fixtures
  helpers: ./tests/helpers
  reports: ./tests/reports

frameworks:
  api:
    enabled: {str(api_enabled).lower()}
    name: {answers.api_framework}
  e2e:
    enabled: {str(e2e_enabled).lower()}
    name: {answers.e2e_framework}

workflow:
  primary_runner: skill
  agent: opencode
  agents:
    claude_code: false
    codex: false

mcp:
  enabled: {str(answers.enable_mcp).lower()}

generation:
  prd_input_mode: prompt
  e2e:
    default_pom: false
    locator_priority:
      - role
      - label
      - testid
      - css
  api:
    prefer_existing_fixtures: true

execution:
  entry: cli
  policy_file: ./.aa/execution-policy.json
  ci_must_use_cli: true
  product_code_roots:
    - app
    - web/src
    - src
  self_healing:
    mode: proposal-only
    allow_assertion_change: false
    allow_product_code_change: false
    allow_auto_merge: false

review:
  require_case_review: true
  require_subplan_review: true
  require_fix_proposal_review: true

archive:
  enable_trace_check: true
  regression_default: true

coverage:
  enabled: true
  mode: pytest-cov             # pytest-cov | server-process
  server_command: ""           # e.g. "uvicorn app:app --port 9999" when mode=server-process
  server_port: 0               # set when mode=server-process
  target_package: app          # --cov=<target_package>
  threshold:
    line: 70
    branch: 60
    module_line: 80
    diff_line: 90
  gate_mode: warn              # warn: below threshold -> PASS_WITH_WARNINGS. block: below -> FAIL.

# Fuzz layer (schemathesis via pytest). Tests live under tests/fuzz/.
fuzz:
  enabled: true
  schema_source: ""            # OpenAPI URL or file

# Performance layer (Locust, absolute thresholds). Locustfiles live under tests/perf/.
performance:
  enabled: true
  base_url: http://localhost:8000
  default_load:
    users: 10
    spawn_rate: 2
    run_time_s: 30
"""


def build_execution_policy(answers: InitAnswers) -> dict:
    api_enabled = answers.api_framework != "none"
    e2e_enabled = answers.e2e_framework != "none"

    targets: list[str] = []
    parallel: dict[str, int] = {}
    retry: dict[str, int] = {}
    if api_enabled:
        targets.append("api")
        parallel["api"] = 4
        retry["api"] = 0
    if e2e_enabled:
        targets.append("e2e")
        parallel["e2e"] = 2
        retry["e2e"] = 1

    policy: dict = {"tier": "local", "targets": targets, "parallel": parallel, "retry": retry}
    if api_enabled:
        policy["api"] = {"timeoutSeconds": 30}
    if e2e_enabled:
        policy["e2e"] = {
            "browsers": ["chromium"],
            "trace": "on-first-retry",
            "screenshot": "only-on-failure",
            "video": "retain-on-failure",
        }
    policy["healing"] = {
        "mode": "proposal-only",
        "maxAttempts": 2,
        "allowAssertionChange": False,
        "allowProductCodeChange": False,
        "allowAutoMerge": False,
        "testChangesOverride": "forbidden",
    }
    return policy


def build_module_map_yaml() -> str:
    return """# Module map for `aa risk context`
# Maps changed file paths to QA modules (supports one-to-many).
#
# rules:
#   - pattern: glob relative to project root
#     modules: [module names matching qa/cases/<module>/]
#     confidence: high | medium | low
#     reason: optional explanation for medium/low mappings

rules:
  - pattern: "backend/app/api/v1/menus/**"
    modules: ["menus"]
    confidence: high

  - pattern: "backend/app/core/auth/**"
    modules: ["users", "roles", "menus"]
    confidence: medium
    reason: "auth middleware affects protected modules"

  - pattern: "backend/app/models/**"
    modules: ["users", "roles", "menus"]
    confidence: low
    reason: "shared persistence model"
"""


def build_data_knowledge_yaml() -> str:
    return """# =============================================================================
# .aa/data-knowledge.yaml - L1 static domain knowledge (human-maintained)
# =============================================================================
#
# This is the FORMAL knowledge base that codegen skills read before generating
# tests. It is a SCAFFOLD created by `aa init`: fill it in before running
# codegen. `aa-api-codegen` / `aa-e2e-codegen` STOP when this file is empty
# of the capability they need.
#
# Do NOT let skills write here directly - planning writes discoveries to
# `qa/changes/<id>/plans/data-knowledge.proposal.<layer>.yaml`; a human promotes
# confirmed entries into this file.
# =============================================================================

version: 1

# Test accounts and their permission levels.
# Never hardcode real production credentials in tests - reference these keys.
accounts: {}

# Auth mechanism and where a valid token comes from.
auth: {}

# Business entities and their known states (menus, users, roles, depts, ...).
entities: {}

# Reusable test-data capabilities. Domain factories describe business-valid
# data independently of a test runner. Adapters own the execution mechanism for
# one test layer and must not be reused across layer boundaries.
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
"""
