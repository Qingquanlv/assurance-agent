# Pure Graph Engine Phase 6: Hard Cutover and Legacy Deletion

**Date:** 2026-08-23

**Status:** Proposed design; implementation not started

**Depends on:** Accepted Phase 5 handoff for the complete Assurance product, including successful provider-live OpenCode and Cursor release gates

**Completes:** The six-phase pure graph-engine extraction

## 1. Decision summary

Phase 6 performs one hard production cut. The `assurance-product` distribution
becomes the sole owner of the `aa` console command. The temporary `aa-next`
name, the legacy `assurance-agent` distribution, the legacy
`assurance-kernel` distribution, and their runtime, product, resource, and
compatibility implementations are deleted in the same phase.

The final runtime is:

```text
aa
└── assurance-product
    ├── graph-engine
    ├── agent-runtime-opencode or agent-runtime-cursor
    └── six installed Assurance capability wheels
```

`graph-engine` remains business-neutral and contains no default graph. An
explicit installed Assurance product supplies the complete workflow and
capability composition. A SUT may supply only the strict data-only project
configuration allowed by Phase 5; it cannot register executable behavior.

Phase 6 does not translate, resume, or import a legacy invocation. Before a
new `aa start` captures its SUT seed, the Assurance product directly deletes
an exact closed set of stale legacy runtime files under the explicitly named
project. It preserves business results such as canonical cases, archives,
tests, reports, Issue history, Improvement history, and organization
configuration.

All agent work remains isolated in engine-owned attempt workspaces. A
successful invocation is delivered only through:

```bash
aa export \
  --engine-root /absolute/engine \
  --invocation-id INVOCATION \
  --destination /fresh/destination \
  <exact product source arguments>
```

The export contains `result-tree/` plus its authenticated manifest, status,
and artifact index. Phase 6 adds no `apply` command and never merges a result
back into the original SUT.

## 2. Entry conditions

Phase 6 implementation may start only after the Phase 5 handoff is complete
and mechanically accepted. The handoff must contain:

1. exact `assurance-product`, six capability-wheel, adapter, and deployment-
   binding wheel identities and digests;
2. the canonical workflow and compiled digest;
3. exact OpenCode and Cursor product entry-point coordinates;
4. the complete 99-alias and 33-route coverage reports;
5. committed-HEAD wheel-isolation evidence;
6. passing deterministic comparison evidence for every required scenario;
7. one complete provider-live OpenCode product benchmark;
8. one complete provider-live Cursor product benchmark;
9. a closed legacy-code and legacy-state deletion inventory;
10. proof that no new wheel imports a legacy package; and
11. no unresolved Critical or Important Phase 5 review finding.

Provider quota exhaustion, missing credentials, invalid credentials, an
unavailable executable, or a benchmark timeout is a blocked gate, not a
successful result. Phase 6 must not weaken, skip, mock, or raise the horizon of
a live gate merely to admit the cutover.

## 3. Goals

Phase 6 must:

1. make `assurance-product` the only distribution that publishes `aa`;
2. remove `aa-next` completely;
3. remove the root `assurance-agent` release artifact and the
   `assurance_agent` Python package;
4. remove `packages/assurance-kernel` and the `assurance_kernel` Python
   package;
5. remove all old workflow/runtime/product ownership and duplicated business
   implementation identified by the Phase 4 and Phase 5 inventories;
6. retain a business-neutral `graph-engine` with no default graph;
7. retain the six installed Assurance capability wheels as the only owners of
   Assurance behavior;
8. retain explicit adapter, deployment-binding, project-configuration, model,
   permission, policy, executable/endpoint, and secret-handle selection;
9. directly delete the exact closed set of legacy per-change runtime state
   before a new start captures its seed;
10. preserve project business artifacts and organization configuration during
    that cleanup;
11. keep execution isolated from the original SUT after cleanup;
12. expose authenticated fresh-destination result export as the only delivery
    mechanism;
13. remove the legacy side of the comparison harness after its evidence is
    frozen;
14. update packaging, documentation, CI, benchmarks, examples, and static
    architecture checks to describe only the final system; and
15. prove that no compatibility bridge or dormant legacy start path remains.

## 4. Non-goals

Phase 6 does not:

- retain an `assurance-agent` launcher shell;
- retain `aa-next` as an alias;
- retain a hidden `aa legacy`, `aa old`, or environment-selected runtime;
- translate a legacy `events.jsonl`, checkpoint, driver pointer, workflow
  state, change ID, or session into a graph-engine invocation;
- resume, replay, inspect, export, or score a legacy invocation after cutover;
- preserve legacy Python import paths or forwarding modules;
- preserve legacy command syntax or exit-code compatibility;
- add a default graph to `graph-engine`;
- add product selection, business gates, prompts, skills, models, or product
  hooks to `graph-engine`;
- scan a SUT for executable plugins;
- allow YAML or project configuration to register operations, handlers,
  validators, effects, or gates;
- copy, patch, merge, or apply the result tree into the original SUT;
- implement three-way merge, drift resolution, rollback journal, or
  `ApplyReceipt` behavior;
- export a running, interrupted, stopped, failed, drifted, or indeterminate
  invocation;
- delete canonical cases, archives, generated tests, reports, Issue or
  Improvement history, memory, policy, knowledge, or other business data;
- delete arbitrary files named like legacy state outside the exact project
  paths in Section 9; or
- claim completion while Phase 5 provider-live evidence remains blocked.

## 5. Chosen cutover approach

### 5.1 Chosen: direct distribution takeover

`packages/assurance-product` changes its console script from `aa-next` to
`aa`. The root repository becomes a non-publishable uv workspace aggregator;
it no longer builds an `assurance-agent` wheel. `assurance_agent/` and
`packages/assurance-kernel/` are deleted rather than wrapped.

This gives the deletion proof a simple shape: a wheel-only environment with
the product, selected adapter, deployment binding, config tree, six capability
wheels, and graph engine runs the complete product while neither legacy
distribution is installed or importable.

### 5.2 Rejected: empty `assurance-agent` launcher shell

A package that merely depends on `assurance-product` and forwards `aa` would
preserve the old distribution name but create a permanent compatibility shell
with no product responsibility. It would also weaken checks for stale imports
and packaging dependencies. Phase 6 therefore removes the distribution.

### 5.3 Rejected: switch first, delete later

Leaving legacy wheels or a hidden legacy command for a later release permits
new legacy starts and makes the cut reversible only through two live runtime
implementations. That contradicts the approved hard-cut boundary.

## 6. Final ownership model

| Concern | Sole owner after Phase 6 |
|---|---|
| graph planning, scheduling, ledger, snapshots, effects, recovery | `graph-engine` |
| OpenCode provider activity | `agent-runtime-opencode` |
| Cursor provider activity | `agent-runtime-cursor` |
| Assurance intake and artifact contracts | `assurance-intake` |
| Assurance generation | `assurance-generation` |
| Assurance execution | `assurance-execution` |
| Assurance healing | `assurance-healing` |
| Assurance quality, Issue, reporting | `assurance-quality` |
| Assurance Retro and Improvement | `assurance-improvement` |
| product graph, installed-source catalog, CLI composition | `assurance-product` |
| deployment bindings and runtime authority | explicit installed binding wheel |
| organization business data | explicit strict project config tree |
| benchmark datasets, scorers, run orchestration | external benchmark harness |

No concern has a legacy fallback owner. An ownership ID has one installed
owner and cannot be re-exported from another wheel.

## 7. Final repository shape

The publishable workspace contains:

```text
packages/
  graph-engine/
  agent-runtime-contracts/
  agent-runtime-opencode/
  agent-runtime-cursor/
  assurance-intake/
  assurance-generation/
  assurance-execution/
  assurance-healing/
  assurance-quality/
  assurance-improvement/
  assurance-product/

examples/
  minimal-product/
  graph-engine-toy-a/
  graph-engine-toy-b/
  agent-runtime-fixture/
  assurance-product-deployment/

benchmark/
  assurance-product/
  specialty/
```

The root `pyproject.toml` is a non-publishable workspace configuration. It has
no `aa` script, `assurance_agent.products` entry point, Hatch build target,
`assurance-kernel` source, or root package dependency. Development groups may
install `assurance-product` and both adapters so repository commands can run
the final CLI.

The following production roots do not exist after the cut:

```text
assurance_agent/
packages/assurance-kernel/
packages/assurance-product/.../aa-next
```

The implementation plan may retain repository-level tests and benchmark
helpers outside production wheels, but they may not import a deleted package
or provide a legacy runtime.

## 8. Final CLI contract

### 8.1 Command owner and name

`packages/assurance-product/pyproject.toml` exposes exactly:

```toml
[project.scripts]
aa = "assurance_product.cli:main"
```

No installed wheel in the supported environment exposes `aa-next` or a second
`aa` provider.

The click program name and help text say `aa`, not `aa-next` or Phase 5. Error
messages, examples, benchmark scripts, shell completion, and documentation use
the final name.

### 8.2 Command surface

Phase 6 preserves the accepted Phase 5 product command surface under the final
name:

```text
aa compile
aa bindings build
aa start
aa run
aa status
aa resume
aa export
aa lock show
```

The old command families are removed rather than forwarded. This includes old
`workflow`, `eval`, `gate`, `risk`, `state`, `skill`, `artifact`, `knowledge`,
`heal`, `report`, `improvement`, `trace`, `doctor`, `init`, `validate`,
`verify`, `decide`, and legacy `run` behavior. Their product capabilities are
reachable through the final product graph and typed entrypoints, not through
legacy runtime commands.

### 8.3 Explicit authority

The final CLI retains Phase 5's explicit authority rules:

- no ambient product/plugin scan;
- no adapter inference or fallback;
- no model fallback;
- no implicit config-tree discovery;
- no implicit credential read;
- no SUT executable plugin load;
- no legacy state lookup; and
- no change-ID-to-invocation-ID inference.

`compile`, `start`, and the first `run` receive exact product, deployment-
binding, project-config, entrypoint, input, engine-root, and required secret
source arguments. `status`, `run`, `resume`, `lock show`, and `export`
authenticate the same exact source coordinates against the invocation lock.

### 8.4 Status and UI

`aa status --json` remains the sole authoritative workflow projection for UI
progress. It derives from the graph ledger, checkpoint, snapshot HEAD, and
bounded adapter evidence. Provider conversation and transcript views remain
provider-owned and auxiliary.

The CLI and UI contain no legacy status adapter and do not combine old and new
invocations in one timeline.

## 9. Legacy project-state cleanup

### 9.1 Trigger

Cleanup runs only as a pre-seed step of a mutating new invocation start:

- `aa start`; or
- `aa run` only when that call creates a new invocation.

It operates only under the exact absolute `--project-dir`. `compile`,
`status`, repeated `run`, `resume`, `lock show`, and `export` do not scan or
delete project files.

Cleanup finishes before SUT seed capture begins. The captured initial tree and
every exported result therefore exclude legacy coordinator state.

### 9.2 Closed deletion set

For each immediate directory `qa/changes/<change-id>/` under the explicit
project root, cleanup directly removes only:

```text
events.jsonl
workflow-state.json
workflow-state.yaml
running-tasks.json
.progression.lock
driver.json
driver.lock
.graph-runtime/
```

The names are matched at that exact depth. Cleanup does not recursively search
for matching basenames elsewhere. `.graph-runtime/` is the only recursively
removed entry and its directory entry must be an ordinary directory owned by
the selected change root.

No backup, trash move, legacy export, or rollback copy is created. A
successful cleanup directly unlinks the selected files and removes the
selected runtime directory.

### 9.3 Explicitly preserved project data

Cleanup preserves at least:

```text
.aa/config.yaml
.aa/policy.yaml
.aa/data-knowledge.yaml
.aa/memory/**
qa/cases/**
qa/archive/**
qa/issues/**
qa/improvements/**
qa/retro/**
qa/changes/<change-id>/cases/**
qa/changes/<change-id>/plans/**
qa/changes/<change-id>/facts/**
qa/changes/<change-id>/review/**
qa/changes/<change-id>/codegen/**
qa/changes/<change-id>/execution/**
qa/changes/<change-id>/healing/**
qa/changes/<change-id>/coverage-repair/**
qa/changes/<change-id>/inspect/**
qa/changes/<change-id>/report/**
tests/**
```

An `events.jsonl` below `qa/issues`, `qa/improvements`, an archived change, or
a nested business artifact directory is business history and is not selected
by the cleanup path rules.

### 9.4 Live legacy activity

The release cutover procedure freezes new legacy starts and drains or
explicitly terminates every known legacy process before installing the final
product.

The cleanup implementation must nevertheless fail closed if exact process-
identity evidence says a legacy driver or task is still live. It must not
delete state out from under a running process and must not send a signal based
only on an untrusted PID. The operator first stops that process through the
approved release procedure, then retries `aa start`.

A stale, dead, malformed, or incomplete legacy pointer grants no resume
authority. After symlink and path checks, it is deleted with the rest of the
closed state set.

### 9.5 Filesystem safety

Cleanup:

- resolves the project root once and keeps descriptor-relative operations
  beneath it;
- never follows a symlink in `qa`, `changes`, a change root, or a selected
  entry;
- rejects selected hard-linked regular files;
- rejects special files, mount crossings, unexpected directory types, path
  races, and a changed inode between inspection and deletion;
- never accepts `..`, an absolute change ID, a glob-selected external path, or
  a project root equal to a broad filesystem root;
- fsyncs every mutated change directory after deletion; and
- either deletes the validated closed set or reports a typed cleanup failure
  before seed capture.

A catchable failure may leave a legal prefix of the deletion set absent.
Retrying cleanup is idempotent: already absent selected entries are success,
while unrelated entries remain untouched.

### 9.6 Cleanup evidence

The start result records a bounded `LegacyCleanupReportV1` containing:

- schema version;
- canonical project identity digest;
- sorted removed relative paths;
- pre-delete kind, size, and content digest for each removed regular file;
- whether the closed runtime directory existed;
- completion status; and
- report digest bound into the new invocation bootstrap.

It records no removed file bytes, legacy events, provider transcript, secret,
PID authority, or compatibility projection. The report is audit evidence for
the destructive cut, not an input that can restore or interpret old state.

## 10. Invocation start and isolation

After cleanup succeeds, start follows the accepted Phase 5 bootstrap exactly:

1. validate canonical product input;
2. capture one stable SUT seed from the explicit project root;
3. resolve the exact installed product, selected adapter, deployment binding,
   project config tree, six capability wheels, and secret handles;
4. authenticate source and contribution identities;
5. create the invocation root under the explicit engine root;
6. bind the cleanup-report digest, root input, lock, and initial tree to the
   bootstrap; and
7. return invocation, composition, lock, input, initial-tree, and cleanup
   identities.

After seed capture, no product task writes the original project. The scheduler
materializes each attempt under:

```text
<engine-root>/invocations/<invocation-id>/workspace/attempts/<attempt-id>/
```

Validated commits become immutable trees under:

```text
<engine-root>/invocations/<invocation-id>/workspace/trees/<tree-id>/
```

`workspace/HEAD.json` is the authoritative committed result pointer. The
initial project and the engine root may not overlap, and the engine root may
not be captured into its own seed.

## 11. Result export

### 11.1 Only delivery mechanism

Phase 6 retains Phase 5's `aa export` implementation and finalizes its command
name. There is no `aa apply`, implicit publish, automatic copy-back, or
post-success merge.

Export requires one explicit invocation ID, engine root, fresh destination,
and the exact product/source arguments needed to authenticate the stored lock.
It does not choose the latest invocation and does not treat a change ID as an
invocation ID.

### 11.2 Exported layout

One successful export contains:

```text
<destination>/
  result-tree/
  manifest.json
  status.json
  artifact-index.json
```

`result-tree/` is an exact materialization of the authenticated final HEAD.
`manifest.json` binds invocation, lock, event-stream, status, result-tree, and
artifact-index identities. The exporter verifies that the ledger projection,
status projection, checkpoint, and snapshot HEAD agree before materializing
bytes.

### 11.3 Destination rules

The destination must be absent or empty and its parent must already exist.
Export:

- refuses a destination inside the engine root, invocation root, or original
  SUT;
- refuses symlinks, hard links, special files, traversal, and path races;
- writes to a private sibling staging directory;
- fsyncs files and directories;
- atomically renames the completed staging directory into place; and
- removes an incomplete staging entry after a catchable failure.

Export never overwrites or merges an existing non-empty destination.

### 11.4 Export eligibility

Only a fully succeeded invocation exports. Running, interrupted, stopped,
failed, drifted, indeterminate, or integrity-invalid invocations fail closed
and produce no result tree. Phase 6 adds no partial or diagnostic result-tree
export.

## 12. Legacy code deletion

### 12.1 Package roots

The implementation deletes:

- `assurance_agent/`;
- `packages/assurance-kernel/`;
- root Hatch build configuration for the old distribution;
- root `aa` script and `assurance_agent.products` entry point;
- `assurance-kernel` workspace membership, source mapping, dependency, type-
  checker path, import-linter contract, and packaging references; and
- every lockfile record reachable only from those packages.

### 12.2 Runtime and product mechanisms

The deletion inventory includes all old implementations of:

- `ProductHooks` and the `assurance_agent.products` protocol/entry-point path;
- workflow graph compiler, planner, scheduler, checkpoint, ledger, workspace,
  and driver code owned by the old runtime;
- operation catalogs and handler dispatch;
- precommit validator catalogs;
- DSL gate built-in catalogs;
- legacy adapter factories, OpenCode/Cursor/headless adapters, process runner,
  driver loop, and detached start logic;
- old artifact registry and duplicate models;
- old product schema, execution-contract, ingest catalog, policy, skill,
  prompt, persona, and agent resources;
- old CLI commands and state projections;
- legacy compatibility re-exports between `assurance_agent` and
  `assurance_kernel`; and
- duplicate business implementations already assigned to one of the six
  capability wheels.

No deletion item may be copied into `assurance-product` merely to keep an old
test green. A behavior still required by the final workflow belongs to its
Phase 4 owner and must already be proven through the installed product before
the deletion commit.

### 12.3 Tests and fixtures

Tests whose sole purpose is to validate a deleted command, import path,
legacy event version, legacy driver, compatibility shim, old product entry
point, or old-vs-new runtime are deleted after the corresponding Phase 5
evidence is frozen.

Reusable business datasets and SUT fixtures may remain, but their runners must
invoke the final `aa` product. Product tests resolve public installed entry
points and must not reconstruct a private registry or import a legacy helper.

## 13. Comparison baseline retirement

Phase 5's old/new comparison harness is migration evidence, not a production
component. Phase 6:

1. freezes the final comparison manifest, projections, dispositions, and
   digests in the Phase 5 handoff;
2. retains the evidence as immutable documentation or release artifacts;
3. removes executable legacy runners and generators;
4. removes legacy fixture copies whose only use is rerunning old code;
5. keeps the runtime-neutral projection schema only if the final benchmark or
   release audit still consumes it; and
6. proves no production wheel imports benchmark code.

The final release gate compares the new product to declared semantic
expectations, not to a runnable legacy implementation.

## 14. Benchmark write model

The final benchmark adopts the Phase 5 isolated write layout and removes the
legacy direct-write loop:

```text
benchmark/assurance-product/results/<provider>-<timestamp>/
  project/                         run-scoped SUT copy and seed source
  config-tree/                     exact data-only project configuration
  deployment.yaml                 closed binding-builder input
  binding-wheel/                  generated and installed binding source
  engine/
    invocations/<invocation-id>/
      ledger/
      workspace/
        attempts/<attempt-id>/     actual agent writes
        trees/<tree-id>/           immutable committed snapshots
        HEAD.json                  authoritative final result
  export/
    result-tree/                   human-consumable final project
    manifest.json
    status.json
    artifact-index.json
  export-validation.json
```

The immutable benchmark fixture is copied to `project/`. `aa start` removes
only the closed legacy runtime set inside that run-scoped copy and captures
the remaining tree as the seed. After seed capture, the benchmark asserts the
project digest remains unchanged. Agent writes occur only in attempt
workspaces; successful candidates advance engine HEAD.

After terminal success, the harness invokes `aa export` into the fresh
`export/` destination and validates:

- manifest and result-tree digests;
- ledger/status/checkpoint/HEAD agreement;
- required business artifact presence and schemas;
- selected API/E2E/Fuzz/Performance branches;
- coverage loop and minimum-coverage result;
- report, Issue, healing, archive, Retro, and Improvement evidence required by
  the case;
- absence of resolved secrets and provider transcript bytes;
- absence of legacy runtime state in the result tree; and
- absence of writes to the seed project after capture.

The harness never copies `export/result-tree/` back over `project/` and never
invokes an apply step.

## 15. Release cutover sequence

The production cut is executed in this order:

1. verify the exact accepted Phase 5 handoff and all source digests;
2. stop admission of new legacy starts in the release environment;
3. enumerate known legacy driver/task activity;
4. allow completed processes to drain and explicitly terminate approved
   remaining processes with audit evidence;
5. verify no known legacy process remains live;
6. build the final product and capability wheels from the accepted committed
   source;
7. rename the product console script to `aa` and remove `aa-next`;
8. remove legacy packages, code, resources, tests, and comparison runners;
9. rebuild the workspace lock and all wheel RECORD data;
10. install the final wheel set into clean OpenCode and Cursor environments;
11. run static no-legacy and wheel-isolation gates;
12. run deterministic repository gates;
13. run one complete provider-live OpenCode benchmark;
14. run one complete provider-live Cursor benchmark;
15. test pre-seed cleanup against seeded stale legacy state;
16. test a complete invocation and `aa export` from each selected product;
17. publish final wheel, command, workflow, benchmark, and deletion evidence;
   and
18. release only if every gate is green.

There is no period in which the final environment exposes both `aa` runtimes.

## 16. Rollback boundary

Rollback means reinstalling the complete previous release and its exact wheel
set before users create new-engine invocations. It is a release rollback, not
a runtime bridge.

Because pre-seed cleanup directly deletes legacy coordinator state, Phase 6
does not promise that a project touched by the new `aa start` can resume an old
invocation after reinstalling the previous release. Preserved business files
remain ordinary project data, but deleted legacy runtime files are not
reconstructed.

The final release contains no switch, adapter, importer, dual writer, or state
translator to make rollback transparent.

## 17. Error handling

The final CLI uses typed fail-closed categories:

| Category | Required behavior |
|---|---|
| Phase 5 handoff incomplete | refuse cutover/release |
| legacy process still live | refuse cleanup and seed capture |
| cleanup path unsafe or raced | refuse start; no seed created |
| cleanup partial legal prefix | report failure; idempotent retry allowed |
| product/source/config drift | refuse composition or invocation open |
| seed capture drift | refuse start |
| provider activity recoverable | retain graph-engine recovery semantics |
| provider activity indeterminate | fail closed; no export |
| ledger/checkpoint/HEAD disagreement | integrity failure; no export |
| destination unsafe/existing | refuse export without overwrite |
| export staging failure | remove catchable staging residue; destination absent |
| legacy command/import attempted | command/import absent, not forwarded |

Errors never select another product, adapter, model, permission profile,
credential, graph, or invocation.

## 18. Security invariants

Phase 6 preserves all prior security boundaries and adds the destructive-
cleanup boundary:

1. only installed authenticated wheels contribute executable behavior;
2. project configuration remains strict and data-only;
3. graph-engine loads no default graph and no SUT executable code;
4. every product and plugin source is explicit and lock-pinned;
5. secret handles are explicit and resolved only through the authorized
   runtime port;
6. provider transcript and chain-of-thought are not copied into graph state or
   exports;
7. task workspaces cannot access ledger, lock, checkpoint, sibling attempt, or
   engine internals;
8. cleanup operates only on exact direct-child runtime paths under an explicit
   project root;
9. cleanup never follows links or interprets legacy event contents;
10. export operates only on an authenticated succeeded HEAD and a fresh safe
    destination; and
11. no deleted package or compatibility module is importable from a final
    wheel-only environment.

## 19. Verification strategy

### 19.1 CLI and packaging

Tests prove:

- `aa` resolves only to `assurance_product.cli:main`;
- `aa-next` is absent;
- root workspace is not a publishable `assurance-agent` distribution;
- `assurance-agent` and `assurance-kernel` are absent from final wheel metadata
  and dependency closure;
- both product extras install and run outside the repository;
- the unselected adapter remains outside the frozen composition; and
- help, completion, docs, examples, and scripts contain no stale command name.

### 19.2 Cleanup

Tests construct every closed legacy path plus similarly named sentinels in
preserved directories. They prove:

- exact runtime files are directly deleted before capture;
- `.graph-runtime/` is deleted recursively and no other directory is;
- canonical business files and nested business ledgers are byte-identical;
- absent paths and repeated cleanup are idempotent;
- live process identity refuses deletion;
- stale/malformed pointer files do not grant resume authority;
- symlink, hardlink, special file, mount, traversal, depth, and rename races
  fail closed;
- partial-deletion fault cuts resume safely; and
- `LegacyCleanupReportV1` exactly matches removed entries and is bound into
  bootstrap identity.

### 19.3 No-legacy static gates

Machine checks reject:

- any filesystem path under deleted package roots;
- imports or strings naming production `assurance_agent` or
  `assurance_kernel` modules;
- `assurance_agent.products` entry points;
- `ProductHooks`, old operation catalogs, legacy workflow driver construction,
  or compatibility re-export patterns;
- `aa-next` scripts, help, examples, or process launches;
- legacy event/checkpoint translation code;
- old/new runtime selection flags or environment variables;
- production dependencies on benchmark code; and
- a default product graph in `graph-engine`.

Allowlisted historical design documents and frozen Phase 5 evidence may name
the deleted system. The allowlist contains exact files, not broad directories
or substring exceptions.

### 19.4 Runtime and export

Tests prove:

- product resolution uses only authenticated installed sources;
- cleanup precedes seed capture;
- original project is unchanged after seed capture;
- all writes occur in attempt workspaces;
- validated candidates alone advance HEAD;
- replay, interrupt/resume, STOP, retry, effects, and terminal receipts retain
  Phase 5 semantics;
- only succeeded invocations export;
- export is exact, atomic, authenticated, secret-free, and idempotently
  rejective of an existing destination; and
- there is no apply or write-back path.

### 19.5 Business workflow

The final installed product proves:

- intake and Explore execute;
- API, E2E, Fuzz, and Performance selection cannot be silently omitted;
- plan/review/codegen/test execution paths use exact typed inputs;
- coverage below policy activates the bounded coverage-repair loop;
- quality report and executive summary are generated when required;
- Issue analysis, healing, archive, Retro, and Improvement execute under their
  declared graph conditions;
- nested business STOP remains distinct from engine failure; and
- all committed artifacts satisfy their schema and cross-artifact validators.

### 19.6 Required repository gates

The final gate set is derived from the post-deletion repository and includes:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
```

The legacy packaging smoke script is deleted or rewritten to inspect only the
final product; it may not install the removed distributions. Provider-live
OpenCode and Cursor benchmarks remain separate mandatory release gates.

## 20. Acceptance criteria

Phase 6 is complete only when:

1. the accepted Phase 5 handoff and both provider-live gates are complete;
2. `assurance-product` is the only provider of `aa`;
3. `aa-next` is absent from wheel metadata, PATH, source, tests, scripts, and
   current documentation;
4. no publishable `assurance-agent` distribution remains;
5. `assurance_agent` and `assurance_kernel` cannot be imported in final clean
   environments;
6. the legacy package roots, ProductHooks, old runtime, operation catalogs,
   compatibility re-exports, CLI, product resources, and obsolete tests are
   deleted;
7. root workspace and lock metadata contain no legacy dependency or source;
8. graph-engine remains business-neutral and has no default graph;
9. all Assurance behavior resolves from the six capability wheels selected by
   the explicit product;
10. OpenCode and Cursor products select exactly one adapter and never fall
    back;
11. a new start deletes only the exact closed legacy runtime paths before seed
    capture;
12. business artifacts and organization configuration remain byte-identical
    through cleanup;
13. unsafe or live legacy state fails closed;
14. post-capture task execution performs no write to the original SUT;
15. every committed task write occurs through engine attempt/snapshot/HEAD
    rules;
16. `aa export` materializes a succeeded authenticated HEAD into a fresh
    separate destination;
17. the exported layout contains exact result tree, manifest, status, and
    artifact index data;
18. no `apply`, write-back, merge, or original-SUT publisher exists;
19. the executable legacy comparison baseline is removed after its evidence is
    frozen;
20. static no-legacy, repository, wheel-isolation, cleanup, fault, and export
    gates pass;
21. one complete final OpenCode benchmark succeeds and exports;
22. one complete final Cursor benchmark succeeds and exports;
23. selected API/E2E/Fuzz/Performance, coverage, report, Issue, healing,
    archive, Retro, and Improvement evidence is present where declared;
24. no secret or provider transcript appears in persisted engine or export
    surfaces;
25. no legacy invocation can be started, resumed, translated, or imported; and
26. release rollback requires reinstalling the previous release rather than
    activating a compatibility path.

## 21. Implementation-plan boundaries

The Phase 6 implementation plan must decompose the hard cut into small,
dependency-ordered commits while keeping the final release atomic:

1. verify and freeze the complete Phase 5 handoff;
2. freeze the exact legacy code, test, command, state, and comparison deletion
   inventories;
3. add the product-owned closed legacy-state cleaner and its adversarial tests;
4. bind cleanup evidence into pre-seed start/bootstrap;
5. rename the product CLI and console script from `aa-next` to `aa`;
6. update final help, command tests, docs, examples, and benchmark launchers;
7. make the root repository a non-publishable workspace and rebuild source and
   lock metadata;
8. delete the legacy `assurance_agent` package and root distribution;
9. delete `assurance-kernel` and every old-runtime dependency/configuration;
10. delete ProductHooks, catalogs, resources, forwarding imports, and duplicate
    business implementation not already removed with those roots;
11. retire legacy-only tests and executable comparison runners after freezing
    evidence;
12. consolidate the final isolated benchmark and export validator;
13. add no-legacy static architecture and wheel-closure gates;
14. run focused package, cleanup, fault, export, and business workflow tests;
15. run full repository and wheel-isolation gates;
16. run the final complete OpenCode benchmark;
17. run the final complete Cursor benchmark; and
18. publish the cutover, deletion, wheel, command, and benchmark evidence.

Each task must:

- begin from an exact public-interface or architecture assertion;
- preserve unrelated dirty worktree changes;
- modify one ownership boundary at a time;
- record focused and full verification evidence;
- receive separate Spec and Standards review;
- commit independently; and
- leave no unresolved Critical or Important finding before its dependent task
  begins.

The plan must not add `aa apply`, a compatibility shell, legacy event parser,
old invocation exporter, default graph, SUT executable plugin path, dual
runtime selector, or hidden fallback.

## 22. Design closure

This design closes the remaining six-phase decisions:

- direct `assurance-product` ownership of `aa`;
- no `aa-next` alias;
- no `assurance-agent` launcher distribution;
- complete deletion of `assurance_agent` and `assurance_kernel` runtime paths;
- no compatibility imports, commands, state bridge, or old invocation resume;
- exact pre-seed deletion of legacy coordinator state only;
- preservation of business artifacts and organization configuration;
- engine-owned isolated execution after cleanup;
- immutable snapshot HEAD as the authoritative result;
- fresh-destination `aa export` as the only delivery path;
- no apply, merge, or original-project write-back;
- retired executable legacy comparison baseline;
- explicit installed product/adapter/binding/config authority;
- no default graph or Assurance semantics in graph-engine; and
- mandatory final OpenCode and Cursor production evidence.

No implementation decision required for Phase 6 is intentionally left open.
A proposal to retain a launcher shell, add a legacy state reader, permit an
in-place result publisher, infer the latest invocation, add a graph-engine
default product, weaken a provider-live gate, or preserve an old command
requires a new design review rather than an implementation-plan deviation.
