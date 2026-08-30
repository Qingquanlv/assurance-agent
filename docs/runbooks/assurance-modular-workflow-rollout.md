# Drain-and-pin: modular Assurance workflow rollout

This runbook is the only supported migration from the pre-modular `assurance-full.yaml` runner to the modular `main.yaml` + Feature-module release (`0.2.0`, deployment plugin `1.1.0`).

Operators must stop new legacy Invocations, retain immutable old environment, finish/export/archive there, start new Invocations on modular release, and send any composition mismatch to the pinned old runner.

The product does not invent a historical-wheel picker. A composition mismatch is an operator action, not a resolver feature.

## 1. Stop new legacy Invocations

Freeze the old environment. Do not start new Invocations on the pre-modular runner after the cutover window opens.

Retain the immutable old environment: the installed `0.1.0` wheels, the old deployment plugin `1.0.0`, and the project `.aa` tree that produced those locks. Do not overwrite those wheel bytes.

## 2. Finish, export, and archive on the pinned old runner

Every in-flight legacy Invocation stays on that pinned old runner until it reaches a public outcome:

1. Drive it to a next transition, interrupt, or terminal.
2. `aa export` the Change.
3. `aa archive` when publication is complete.

Do not resume a legacy lock with the modular runner. If an operator does, the new runner fail-closes and leaves ledger bytes unchanged. Send that composition mismatch back to the pinned old runner.

## 3. Start new Invocations on the modular release

New work uses the modular Product:

- Product root `resources/workflow/main.yaml`
- six authenticated Feature modules
- deployment plugin `assurance.product.agent` `1.1.0`

Install `graph-engine`, `agent-runtime-contracts`, the six Features, and `assurance-product` at `0.2.0`. Concrete Client wheels stay at their existing versions unless their API changed.

## 4. Composition mismatch

A modular runner opening a legacy lock (or the reverse) is a composition mismatch. The ledger is not appended. Return the Invocation to the pinned old runner, finish/export/archive there, then start a new Invocation on the modular release if more work is required.

Do not invent a historical-wheel picker. Operators choose the runner that matches the lock.
