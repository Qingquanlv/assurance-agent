#!/usr/bin/env bash

# Helpers kept side-effect free until explicitly called so unit tests can source
# this file without launching the benchmark loop.

remove_generated_artifact_tree() {
  local target="$1"
  [ -e "$target" ] || return 0
  chmod -R u+w -- "$target" 2>/dev/null || true
  rm -rf -- "$target"
  [ ! -e "$target" ]
}

select_retro_shell_change_id() {
  local changes_root="$1"
  shift
  local row cid term detail archive_field

  for row in "$@"; do
    IFS='|' read -r cid term detail archive_field <<<"$row"
    if [ "$archive_field" = "archived=yes" ] && [ -d "$changes_root/$cid" ]; then
      printf '%s' "$cid"
      return 0
    fi
  done
  for row in "$@"; do
    IFS='|' read -r cid term detail archive_field <<<"$row"
    if [ "$term" = "completed" ] && [ -d "$changes_root/$cid" ]; then
      printf '%s' "$cid"
      return 0
    fi
  done
  # A failed/stopped change from this run is still the correct session shell:
  # Retro writes project artifacts and only needs a live change RuntimeContext.
  for row in "$@"; do
    IFS='|' read -r cid term detail archive_field <<<"$row"
    if [ -d "$changes_root/$cid" ]; then
      printf '%s' "$cid"
      return 0
    fi
  done

  local newest
  newest="$(find "$changes_root" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | sort | tail -1)"
  [ -n "$newest" ] || return 1
  basename "$newest"
}

retro_artifacts_complete() {
  local retro_dir="$1" dry_run="$2" context signal_count required
  context="$retro_dir/context.json"
  [ -s "$context" ] || return 1
  signal_count="$(python3 -c '
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get("signal_count")
if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise SystemExit(1)
print(value)
' "$context" 2>/dev/null)" || return 1
  if [ "$dry_run" = "true" ] || [ "$signal_count" = "0" ]; then
    return 0
  fi
  for required in proposal-candidates.json retro-summary.md accept-status.json; do
    [ -s "$retro_dir/$required" ] || return 1
  done
}

retro_result_is_closed() {
  local retro_exit="$1" artifacts_captured="$2"
  [ "$retro_exit" = "0" ] && [ "$artifacts_captured" = "true" ]
}

benchmark_result_exit_code() {
  local do_archive="$1" do_retro="$2" retro_exit="$3" retro_closed="$4"
  shift 4
  local row cid term detail archive_field failed=0

  [ "$#" -gt 0 ] || return 1
  for row in "$@"; do
    IFS='|' read -r cid term detail archive_field <<<"$row"
    if [ "$term" != "completed" ]; then
      failed=1
    elif [ "$do_archive" = "true" ] && [ "$archive_field" != "archived=yes" ]; then
      failed=1
    fi
  done
  if [ "$do_retro" = "true" ] && ! retro_result_is_closed "$retro_exit" "$retro_closed"; then
    failed=1
  fi
  return "$failed"
}
