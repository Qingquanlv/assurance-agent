#!/usr/bin/env bash
# Start a per-workspace OpenCode + OpenChamber pair.
# Does not start the SUT and does not invoke aa bootstrap.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"

DEST="${1:-${SUT_WORKSPACE:-}}"
if [[ -z "${DEST}" ]]; then
  echo "fail-closed: workspace path is required" >&2
  exit 1
fi
DEST="$(cd "${DEST}" && pwd)"

OPENCHAMBER_WEB="${OPENCHAMBER_WEB:-/Users/lvqingquan/openchamber/packages/web}"
AA_BIN="${OPENCHAMBER_AA_BIN:-/Users/lvqingquan/agent/assurance-agent/.venv/bin/aa}"
RUNTIME_DIR="${DEST}/.aa/runtime"
STATE="${RUNTIME_DIR}/workspace-runtime.json"
OPERATOR_ENV="${RUNTIME_DIR}/operator.env"
mkdir -p "${RUNTIME_DIR}"
if [[ ! -f "${OPERATOR_ENV}" ]]; then
  python3 - "${OPERATOR_ENV}" <<'PY'
from pathlib import Path
import secrets
import sys
path = Path(sys.argv[1])
token = secrets.token_urlsafe(32)
path.write_text(
    f"AA_NEXT_OPENCODE_TOKEN={token}\n"
    "QA_ADMIN_PASSWORD=123456\n"
)
path.chmod(0o600)
PY
fi
set -a
# shellcheck disable=SC1090
source "${OPERATOR_ENV}"
set +a

if ! command -v opencode >/dev/null 2>&1; then
  echo "fail-closed: opencode is not on PATH" >&2
  exit 1
fi
if [[ ! -f "${OPENCHAMBER_WEB}/server/index.js" ]]; then
  echo "fail-closed: missing OpenChamber server at ${OPENCHAMBER_WEB}/server/index.js" >&2
  exit 1
fi
if ! command -v bun >/dev/null 2>&1; then
  echo "fail-closed: bun is not on PATH" >&2
  exit 1
fi

allocate_two_ports() {
  python3 - <<'PY'
import socket

first = socket.socket()
second = socket.socket()
first.bind(("127.0.0.1", 0))
second.bind(("127.0.0.1", 0))
ports = first.getsockname()[1], second.getsockname()[1]
if ports[0] == ports[1]:
    raise SystemExit("fail-closed: kernel returned the same port twice")
print(ports[0], ports[1])
first.close()
second.close()
PY
}

http_ready() {
  local url="$1"
  curl -fs -o /dev/null --max-time 2 "${url}"
}

wait_http() {
  local url="$1"
  local tries="${2:-50}"
  local i
  for i in $(seq 1 "${tries}"); do
    if http_ready "${url}"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

pid_alive() {
  local pid="$1"
  [[ -n "${pid}" && "${pid}" != "null" ]] && kill -0 "${pid}" 2>/dev/null
}

if [[ -f "${STATE}" ]]; then
  existing_oc_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("opencode_pid") or "")' "${STATE}")"
  existing_ch_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("openchamber_pid") or "")' "${STATE}")"
  existing_oc_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("opencode_url") or "")' "${STATE}")"
  existing_ch_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("openchamber_url") or "")' "${STATE}")"
  if pid_alive "${existing_oc_pid}" && pid_alive "${existing_ch_pid}" \
    && http_ready "${existing_oc_url}/" && http_ready "${existing_ch_url}/"; then
    echo "reusing workspace runtimes"
    echo "OPENCODE_URL=${existing_oc_url}"
    echo "OPENCHAMBER_URL=${existing_ch_url}"
    echo "SUT_WORKSPACE=${DEST}"
    exit 0
  fi
fi

read -r OC_PORT CH_PORT < <(allocate_two_ports)
OC_URL="http://127.0.0.1:${OC_PORT}"
CH_URL="http://127.0.0.1:${CH_PORT}"

launch_detached() {
  local cwd="$1"
  local log="$2"
  shift 2
  DEST="${cwd}" python3 - "$log" "$@" <<'PY'
import os
import subprocess
import sys

log_path, *command = sys.argv[1:]
cwd = os.environ["DEST"]
os.makedirs(os.path.dirname(log_path), exist_ok=True)
with open(log_path, "ab", buffering=0) as log:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print(process.pid)
PY
}

OC_PID="$(launch_detached "${DEST}" "${RUNTIME_DIR}/opencode.log" \
  opencode serve --hostname 127.0.0.1 --port "${OC_PORT}")"
echo "${OC_PID}" >"${RUNTIME_DIR}/opencode.pid"

if ! wait_http "${OC_URL}/global/health" 40 && ! wait_http "${OC_URL}/" 20; then
  echo "fail-closed: OpenCode did not become ready at ${OC_URL}" >&2
  echo "---- opencode.log ----" >&2
  tail -n 40 "${RUNTIME_DIR}/opencode.log" >&2 || true
  exit 1
fi

export OPENCODE_SKIP_START=true
export OPENCODE_PORT="${OC_PORT}"
export OPENCODE_HOST="${OC_URL}"
export OPENCHAMBER_PORT="${CH_PORT}"
export OPENCHAMBER_DATA_DIR="${RUNTIME_DIR}/openchamber-data"
export OPENCHAMBER_AA_BIN="${AA_BIN}"
CH_PID="$(launch_detached "${OPENCHAMBER_WEB}" "${RUNTIME_DIR}/openchamber.log" \
  bun "${OPENCHAMBER_WEB}/server/index.js" --port "${CH_PORT}")"
echo "${CH_PID}" >"${RUNTIME_DIR}/openchamber.pid"

if ! wait_http "${CH_URL}/" 50; then
  echo "fail-closed: OpenChamber did not become ready at ${CH_URL}" >&2
  echo "---- openchamber.log ----" >&2
  tail -n 40 "${RUNTIME_DIR}/openchamber.log" >&2 || true
  exit 1
fi

python3 - <<PY
import json
from pathlib import Path
Path("${STATE}").write_text(json.dumps({
    "workspace": "${DEST}",
    "opencode_pid": ${OC_PID},
    "opencode_url": "${OC_URL}",
    "openchamber_pid": ${CH_PID},
    "openchamber_url": "${CH_URL}",
}, indent=2) + "\n")
PY

if ! pid_alive "${OC_PID}" || ! pid_alive "${CH_PID}" || ! http_ready "${CH_URL}/"; then
  echo "fail-closed: workspace runtimes exited after start" >&2
  tail -n 40 "${RUNTIME_DIR}/opencode.log" >&2 || true
  tail -n 40 "${RUNTIME_DIR}/openchamber.log" >&2 || true
  exit 1
fi

echo "OPENCODE_URL=${OC_URL}"
echo "OPENCHAMBER_URL=${CH_URL}"
echo "SUT_WORKSPACE=${DEST}"
echo "Open this OpenChamber and select SUT_WORKSPACE. Do not run aa bootstrap from this job."
