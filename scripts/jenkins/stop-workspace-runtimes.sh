#!/usr/bin/env bash
# Stop the per-workspace OpenCode + OpenChamber pair.
# Does not stop Jenkins, the SUT, or aa bootstrap.
set -euo pipefail

DEST="${1:-${SUT_WORKSPACE:-/Users/lvqingquan/agent/workspaces/vue-fastapi-admin}}"
DEST="$(cd "${DEST}" && pwd)"
STATE="${DEST}/.aa/runtime/workspace-runtime.json"

if [[ ! -f "${STATE}" ]]; then
  echo "nothing to stop: missing ${STATE}"
  exit 0
fi

python3 - "${STATE}" <<'PY'
import json
import os
import signal
import sys
import time

state_path = sys.argv[1]
state = json.load(open(state_path))
stopped = []
for key in ("openchamber_pid", "opencode_pid"):
    pid = state.get(key)
    if not isinstance(pid, int):
        continue
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    stopped.append(f"{key}={pid}")

print("stopped " + (", ".join(stopped) if stopped else "nothing running"))
os.remove(state_path)
print(f"removed {state_path}")
PY
