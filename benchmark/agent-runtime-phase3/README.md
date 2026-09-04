# Phase 3 provider-live adapter benchmarks

These scripts are release evidence for one already-frozen fixture item. They are not the crash-safety oracle and not an `aa` cutover.

`manifest.json` is the only source of fixture digests, graph/entrypoint, canonical request, workspace write-set, result schema, expected output digest, adapter profile, and external tool identity. The scripts do not rewrite it and do not choose fallback values.

## Run

```bash
bash benchmark/agent-runtime-phase3/run-opencode.sh
```

The script fails closed if the pinned OpenCode server is missing, drifted, or does not advertise the pinned protocol profile. A missing prerequisite is a non-zero exit, never a silent skip or fake success.

Deterministic fakes in `tests/agent_runtime` remain authoritative for ambiguous network and process cuts.
