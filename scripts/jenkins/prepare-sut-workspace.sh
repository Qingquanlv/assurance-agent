#!/usr/bin/env bash
# External Jenkins job body: create a SUT workspace and materialize .aa/run-spec.yaml.
# Does not start the SUT and does not invoke aa bootstrap.
set -euo pipefail

GITHUB_URL="${GITHUB_URL:-${SUT_REPO:-https://github.com/Qingquanlv/vue-fastapi-admin.git}}"
BRANCH="${BRANCH:-${SUT_REF:-main}}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/Users/lvqingquan/agent/workspaces}"
repo_basename() {
  local url="${1%/}"
  url="${url%.git}"
  basename "${url}"
}
WORKSPACE_NAME="${WORKSPACE_NAME:-$(repo_basename "${GITHUB_URL}")}"
OVERWRITE_RUN_SPEC="${OVERWRITE_RUN_SPEC:-false}"
START_RUNTIMES="${START_RUNTIMES:-true}"
DEST="${WORKSPACE_ROOT}/${WORKSPACE_NAME}"
SUT_REPO="${GITHUB_URL}"
SUT_REF="${BRANCH}"
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"

mkdir -p "${WORKSPACE_ROOT}"
if [[ ! -d "${DEST}/.git" ]]; then
  git clone --branch "${SUT_REF}" "${SUT_REPO}" "${DEST}"
else
  git -C "${DEST}" fetch --tags origin
  git -C "${DEST}" checkout "${SUT_REF}"
  if git -C "${DEST}" rev-parse --verify --quiet "origin/${SUT_REF}"; then
    git -C "${DEST}" reset --hard "origin/${SUT_REF}"
  fi
fi

if [[ ! -f "${DEST}/.aa/policy.yaml" || ! -f "${DEST}/.aa/data-knowledge.yaml" ]]; then
  echo "fail-closed: ${DEST} is missing .aa/policy.yaml or .aa/data-knowledge.yaml" >&2
  exit 1
fi

SPEC="${DEST}/.aa/run-spec.yaml"
if [[ -f "${SPEC}" && "${OVERWRITE_RUN_SPEC}" != "true" ]]; then
  echo "keeping existing ${SPEC}"
else
  cat > "${SPEC}" <<'EOF'
schema_version: "1"
product: assurance-opencode
entrypoint: full
requirement: |
  Cover dept CRUD on /api/v1/dept.
  Cover POST /api/v1/dept/create including the 20-character name boundary.
  Cover GET /api/v1/dept/list tree query, update, delete, and admin permission checks.
candidate_test_families:
  - api
case_modules:
  - system/dept
sut:
  base_url: http://127.0.0.1:9999
  readiness_url: http://127.0.0.1:9999/openapi.json
  env:
    BASE_URL: http://127.0.0.1:9999
  env_from_node:
    - QA_ADMIN_PASSWORD
routes:
  provider_model: deepseek/deepseek-v4-flash
  worker_profile: max
budgets:
  review_rounds: 4
  coverage_rounds: 2
  healing_rounds: 2
  execution_retries: 2
opencode_token_env: AA_NEXT_OPENCODE_TOKEN
timeout_seconds: 28800
EOF
  echo "wrote ${SPEC}"
fi

echo "SUT_WORKSPACE=${DEST}"
if [[ "${START_RUNTIMES}" == "true" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SUT_WORKSPACE="${DEST}" "${SCRIPT_DIR}/start-workspace-runtimes.sh" "${DEST}"
else
  echo "START_RUNTIMES=false; skipped OpenCode/OpenChamber"
fi
