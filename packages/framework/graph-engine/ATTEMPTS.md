# Attempt modules

An Attempt completes one execution of a graph node. LangGraph chooses the node;
the Attempt runtime dispatches its persisted phase; handlers perform the work and
commit progress. These directories group those responsibilities without adding
another execution layer.

```text
attempts/
├── __init__.py                 Public type exports
├── orchestration/
│   ├── node_factory.py         LangGraph node wrapper and technical retry loop
│   ├── kernel.py               Runtime and handler composition
│   ├── runtime.py              Dispatch from checkpoint.phase
│   ├── checkpoint.py           Persisted phase and complete Attempt record
│   ├── checkpoint_bridge.py    Anchor system interrupts to graph checkpoints
│   ├── handlers.py             Authorize, execute, reconcile, terminate, release
│   └── commit.py               Validate and promote sealed writes
├── resources/
│   ├── workspace.py            Project/write roots, sealing and promotion receipts
│   ├── resource_arbiter.py     Read/write authorization and overlap checks
│   ├── secret_sources.py       Authorized secret handles and source resolution
│   └── activity.py             External activity identity, progress and recovery
├── models/
│   ├── keys.py                 AttemptKey and BusinessActivation
│   ├── contracts.py            Inputs, outputs, paths, retries and timeouts
│   ├── context.py              Execution context and authorized scope
│   ├── resolutions.py         Committed, rejected, failed, pending, indeterminate
│   ├── errors.py               Identity and integrity errors
│   └── runtime_evidence.py     Read-only invocation evidence port
└── execution_host/
    ├── production_host.py     Subprocess supervision and cleanup
    ├── production_worker.py   Installed business-handler execution
    ├── host_protocol.py       Request/result envelopes and authenticated transport
    └── host_receipts.py       Durable completion receipts
```

Callers can continue to import public types from `graph_engine.attempts`, for
example `TaskAttemptContract`, `AttemptKey`, and `AttemptCheckpoint`. Internal
module imports use the owning directory, such as
`graph_engine.attempts.orchestration.kernel`. Directory packages do not eagerly
import implementations or register handlers.

The checkpoint stores remain in `graph_engine.persistence` and
`assurance_product.sqlite_attempt_checkpoint`. Product runtime ports assemble the
host and the Attempt kernel. Invocation admission and stopping the AA worker
remain product responsibilities.
