# OpenCode provider-live benchmark

The Phase 5 live item drives the **real SUT**. The Assurance result is the
change directory, not a harness result tree.

```text
<sut>/qa/changes/<change-id>/
```

`results/<run>/` holds only harness diagnostics: logs, timing, isolated
wheels/venv, and `evidence.json`. It is not an alternate result source and
must not contain `project/`, `export/`, `workspace/`, `HEAD.json`, a latest
pointer, or a result registry.

## Run identity

A run derives a unique deterministic change ID from item ID + stamp + nonce:

```text
BENCH-<item-id>-<stamp>-<nonce>
```

The driver invokes:

```text
aa start|run|status --project-dir <sut> --change <id>
aa export --project-dir <sut> --change <id>   # only after achieved
```

Export is called once after the change is achieved. Failure leaves original
SUT tests unchanged and does not publish.

## Evidence fields

Live evidence records `sut_root`, `change_id`, `change_root`, terminal
status, publish receipt, provider session/process reference, and logs.
Those fields are diagnostics for the harness, not a second result tree.

## Routing

Every OpenCode prepare ID is locked to `openai/gpt-5.6-terra` / `max`.
Product-installed locked OpenCode profiles are preflighted; the live run
does not install OpenCode configuration into the SUT.

## Commands

```bash
benchmark/assurance-product-phase5/run-opencode.sh
benchmark/assurance-product-phase5/run-cursor.sh
```
