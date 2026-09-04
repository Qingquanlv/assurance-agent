#!/usr/bin/env bash
# Poll the OpenCode benchmark loop until the supervisor exits or BENCHMARK_LOOP_DONE appears.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${WATCH_LOG:-$ROOT/benchmark/resume-logs/opencode-loop-latest.log}"
PID_FILE="$ROOT/benchmark/resume-logs/opencode-loop-latest.pid"
INTERVAL="${WATCH_INTERVAL:-30}"

log_status() {
  local pid="$1"
  printf '[%s] supervisor pid=%s' "$(date +%H:%M:%S)" "${pid:-none}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    printf ' running'
  else
    printf ' stopped'
  fi
  if [ -f "$LOG" ]; then
    local last
    last="$(tail -1 "$LOG" 2>/dev/null || true)"
    [ -n "$last" ] && printf ' | %s' "$last"
  fi
  printf '\n'
}

echo "watching $LOG (interval=${INTERVAL}s)"
while true; do
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  log_status "$pid"
  if [ -f "$LOG" ] && grep -q 'opencode benchmark loop done' "$LOG" 2>/dev/null; then
    echo "done marker found"
    tail -20 "$LOG"
    exit 0
  fi
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    echo "supervisor not running"
    tail -30 "$LOG" 2>/dev/null || true
    exit 1
  fi
  sleep "$INTERVAL"
done
