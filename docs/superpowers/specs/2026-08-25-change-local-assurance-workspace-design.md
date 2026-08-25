# Change-local Assurance Workspace and Achieved Publish Design

**Status:** approved on 2026-08-26

**Date:** 2026-08-25

**Scope:** replace the Phase 5 whole-SUT snapshot workspace and Phase 6 whole-tree export model

## 1. Decision

The Assurance product uses `qa/changes/<change-id>/` as the durable unit of
work. The original SUT remains readable but is not mutated while a workflow is
running. Agent output is staged inside the active change, validated, and then
promoted into canonical change artifacts. Only an achieved change may publish
its approved generated files back to the original SUT.

The graph engine does not own an Assurance-specific tree store, change layout,
or default graph. It retains generic scheduling, activity recovery, receipts,
and ledger semantics. The installed Assurance product owns change paths,
artifact validation, family merge rules, and final publication.

This decision supersedes the following Phase 5 requirements:

- copying the full SUT into `results/<run>/project` as the execution project;
- storing every successful task state under `workspace/trees/<digest>`;
- using `workspace/HEAD.json` as the product result authority;
- materializing the full final SUT into `results/<run>/export`; and
- forbidding publication to the original SUT after terminal success.

It also supersedes any Phase 6 requirement that treats a whole-tree export as
the normal Assurance delivery interface.

The `full` entrypoint stops at achieved assurance status. It no longer runs
archive inside the same graph, because publication must happen after achieved
status and before archive. `auto_archive` is removed from the product input.
Publication and archive are explicit, ordered commands:

```text
aa workflow run ...  -> achieved
aa export            -> generated files published to the SUT
aa archive           -> change record archived when requested
```

## 2. Historical basis

Before commit `51f385c` (`feat(workflow): isolate task writes in content
store`), the workflow already used `qa/changes/<change-id>/` for proposals,
cases, plans, reviews, execution evidence, reports, events, and archive input.
The skills and workflow schema treated the change directory as the workflow's
business workspace.

That older implementation is the structural reference, not a source-level
rollback. It also let code generation write directly to `tests/**`, which meant
a failed agent could leave the original SUT dirty. This design restores the
change-centered model while adding a narrow stage/validate/promote protocol.

OpenSpec uses the same high-level model: one active change directory contains
the proposal and related artifacts; apply/archive occurs only after the change
is ready. Assurance keeps its richer typed artifacts and execution evidence,
but does not require a second whole-project representation.

## 3. Goals

1. Do not mutate original SUT files during an incomplete or failed workflow.
2. Do not expose partial output from a failed or retried node as canonical
   change data.
3. Let API, E2E, Fuzz, and Performance branches run concurrently without
   overwriting one another.
4. Preserve deterministic validation, durable activity recovery, audit events,
   and idempotent final publication.
5. Make `qa/changes/*` the only active-result collection OpenChamber needs to
   scan.
6. Remove whole-SUT copies, content-addressed trees, and HEAD indirection from
   the product result model.

## 4. Non-goals

- Supporting concurrent benchmark runs against the same SUT and same change ID.
- Providing a general version-control system inside graph-engine.
- Loading handlers, validators, or executable plugins from the SUT.
- Restoring direct agent writes to original `tests/**`.
- Preserving Phase 5 result-directory compatibility.
- Keeping a separate benchmark run registry or `latest-run` pointer.
- Keeping `auto_archive` or an archive branch inside `full`.

## 5. Canonical directory layout

```text
<project-root>/
├── qa/
│   ├── changes/
│   │   └── <change-id>/
│   │       ├── .qa.yaml
│   │       ├── proposal.md
│   │       ├── status.json
│   │       ├── events.jsonl
│   │       ├── explore/
│   │       ├── cases/
│   │       ├── plans/
│   │       ├── review/
│   │       ├── codegen/
│   │       ├── execution/
│   │       ├── inspect/
│   │       ├── healing/
│   │       ├── report/
│   │       ├── generated/
│   │       │   ├── api/files/<repo-relative-path>
│   │       │   ├── e2e/files/<repo-relative-path>
│   │       │   ├── fuzz/files/<repo-relative-path>
│   │       │   └── performance/files/<repo-relative-path>
│   │       ├── apply-manifest.json
│   │       ├── .runtime/
│   │       │   ├── ledger/
│   │       │   ├── activities/
│   │       │   └── receipts/
│   │       └── .staging/
│   │           ├── <node-id>/<attempt-id>/
│   │           └── execution/<batch-id>/
│   ├── cases/
│   └── archive/
└── tests/                 # unchanged until achieved publish
```

`generated/<family>/files/` mirrors original project-relative target paths.
For example, an API candidate for `tests/api/test_dept.py` is stored at:

```text
qa/changes/<id>/generated/api/files/tests/api/test_dept.py
```

The mirrored path keeps existing plans and generated-file manifests expressed
in normal repository-relative paths while giving each concurrent family its
own physical namespace.

`.staging/` is ephemeral and non-canonical. OpenChamber, validators, later
workflow nodes, reports, and export ignore it. A crash may leave a staging
directory physically present; resume either reuses the exact authenticated
attempt or deletes it before creating a replacement. It is never evidence of a
successful node.

`.runtime/` contains the generic engine ledger, activity identities, and
durable receipts for this invocation. The Assurance product supplies this
invocation root to the engine; graph-engine does not derive or interpret the
`qa/changes` path. `events.jsonl` and `status.json` are product projections for
users and OpenChamber, while `.runtime/ledger` remains execution authority.

## 6. Module seams

### 6.1 Graph-engine

Graph-engine exposes a small generic task workspace interface:

```python
class TaskWorkspace(Protocol):
    @property
    def read_root(self) -> Path: ...

    @property
    def write_root(self) -> Path: ...

    @property
    def identity(self) -> TaskWorkspaceIdentity: ...
```

The engine:

- authenticates the invocation, task, attempt, read root, and write root;
- launches the selected installed adapter with the SUT readable and only the
  write root writable;
- records activity and terminal receipts;
- never interprets `qa`, `changes`, test families, or generated-file manifests;
- does not create `trees/`, `HEAD.json`, or a whole-SUT candidate copy.

Installed product and `TaskHandler` Python code is part of the trusted runtime
boundary. Root descriptors and pinned directory identities authenticate host
protocol inputs and reject persistent namespace replacement, but they are not
an OS sandbox against a same-permission malicious installed handler that swaps
and restores a pathname during its own call. The SUT cannot register that code.
Provider/model processes remain untrusted and are confined separately as
described below.

The ledger remains the authority for graph progress. It is not a filesystem
version store.

### 6.2 Assurance product

The installed Assurance product provides a deep `ChangeWorkspace` module. Its
interface is limited to:

```python
begin_attempt(change, node, attempt) -> TaskWorkspace
promote_attempt(workspace, result, validators) -> PromotionReceipt
discard_attempt(workspace) -> None
build_execution_view(change, families) -> ExecutionView
publish_achieved(change, project) -> PublishReceipt
```

Behind this interface it owns logical-path resolution, schemas, family
namespaces, staging cleanup, digest checks, collision detection, atomic file
replacement, and recovery journals. Product handlers and tests use this
interface rather than constructing change paths independently.

The existing installed-wheel rule remains: project configuration cannot
register handlers, operations, effects, or validators.

## 7. Node attempt lifecycle

### 7.1 Begin

Before dispatch, the Assurance product creates exactly one staging directory:

```text
qa/changes/<id>/.staging/<node-id>/<attempt-id>/
```

The adapter receives:

- original project root as readable project context;
- canonical change artifacts from prior successful nodes as readable input;
- the exact staging directory as its writable root; and
- typed output instructions that map logical outputs to staging paths.

The host rejects writes outside the staging root. It does not depend only on
the model obeying prompt instructions.

### 7.2 Validate

When the adapter reports success, finalize parses the structured result and
builds the candidate file set from the staging directory. Existing typed
schemas, capability-leaf validation, closed generated-file mappings, review
gates, write scopes, and cross-artifact checks run against that candidate.

Validation reads prior canonical change artifacts plus staged candidate files.
It never reads a partially promoted target.

### 7.3 Promote

If validation succeeds, the product promotes only declared outputs:

- ordinary artifacts go to their existing canonical change paths;
- generated repository files go to
  `generated/<family>/files/<repo-relative-path>`; and
- a canonical promotion receipt records node ID, attempt ID, input digests,
  output digests, and target paths.

Promotion stages complete target directories or files and uses `os.replace`
plus directory fsync. Each file replacement is atomic. A pending intent is
durable before the first replacement, and the completed receipt is durably
published after every replacement succeeds and before any adjacent temporary,
rollback, or pending-intent cleanup. Repeating the same authenticated promotion
is idempotent. Different bytes for an already promoted attempt fail closed.

The required direct `qa/changes/<id>` file layout has no single OS primitive
that atomically switches an arbitrary set of files spanning directories.
Therefore “atomic promotion” at the batch level means terminal-failure
atomicity: an ordinary failed terminal outcome is allowed only after every
canonical target is proven at its baseline; otherwise the attempt stays
prepared and raises `PromotionPublicationIndeterminate` until authenticated
replay completes or restores it. Engine-managed workflow readers and successors
hold the resource dependency and consume outputs only after the completed
receipt and terminal event. A raw filesystem observer that ignores receipts can
see a short intermediate set during the successful replace window. This is the
explicit direct-layout tradeoff; no generation pointer, snapshot tree, or HEAD
indirection is introduced.

### 7.4 Failure and retry

If adapter execution, structured-result parsing, or validation fails:

- no canonical change artifact is replaced;
- no original SUT file is changed;
- the attempt is recorded as failed in the ledger and change event projection;
- its staging directory is removed when safe, otherwise ignored and cleaned on
  resume; and
- a retry receives a new attempt ID and a fresh staging directory.

Previously promoted outputs from successful nodes remain available for resume
and audit. A failed workflow therefore remains visible in OpenChamber without
presenting failed-node half-products as valid data.

A filesystem or process failure during canonical replacement, rollback, or
completed-receipt publication is not an ordinary node failure when the store
cannot prove the complete baseline. The ledger retains the prepared commit,
the pending intent and authenticated rollback evidence remain recoverable, and
resource locks prevent successors from consuming the in-flight set. Replay
finishes or restores the promotion without re-executing the handler.

## 8. Parallel generation and merge rules

API, E2E, Fuzz, and Performance candidates use separate physical family
directories. They may be generated and promoted concurrently.

Every generated-file record keeps its intended repository-relative target.
Before execution or achieved publish, the product builds a closed merged view:

1. collect only files named by validated family manifests;
2. reject files present on disk but absent from the closed mapping;
3. group records by repository-relative target;
4. accept one owner;
5. accept multiple owners only when bytes, digest, mode, and declared operation
   are identical; and
6. reject conflicting candidates before running or publishing anything.

Shared paths such as `tests/testdata/**` therefore cannot be silently won by
the last branch to finish.

## 9. Execution before publish

Tests must run before the original SUT is mutated. `build_execution_view()`
creates an ephemeral test view from:

- existing SUT test files;
- the validated merged generated-file set; and
- the exact selected-family manifests.

The view contains only the test/support tree needed by the runner, not a copy
of the SUT. Product code continues to load from the original read-only project
root. Candidate files shadow their corresponding existing test files in the
execution view. Pytest root/config/import paths and other runner paths are
resolved explicitly so test discovery does not depend on the physical change
directory.

Execution views live under `.staging/execution/<batch-id>`, are addressed by
batch ID, and are disposable. They are not snapshots, not product results, and
not read by OpenChamber. Execution evidence is written canonically under
`execution/` only after parser validation.

## 10. Achieved publish and `aa export`

An achieved change has:

- terminal workflow success;
- no pending activity, effect, interrupt, or retry;
- passing required review and quality gates;
- complete selected-family evidence;
- a validated merged generated-file set; and
- a canonical `apply-manifest.json` binding every target to its source digest
  and expected pre-publish target state.

`aa export` is the only normal command that publishes generated files to the
original SUT. It operates on the current or explicitly selected change and:

1. rejects a non-achieved change;
2. revalidates all manifest and source digests;
3. rejects target drift relative to the manifest baseline;
4. writes adjacent temporary files and records a small publish journal;
5. replaces only declared target files;
6. resumes or rolls back an interrupted publish from that journal;
7. writes an authenticated publish receipt into the change; and
8. leaves the change directory as the workflow record.

The publication transaction is scoped to the declared file set. It does not
capture or materialize the whole project and does not maintain historical SUT
trees.

Calling `aa export` again with the same achieved change is idempotent. A target
already matching the declared digest is accepted. A target containing other
bytes is drift and requires an explicit new change; it is never overwritten
silently.

## 11. Benchmark behavior

The Phase 5 benchmark runs against its real SUT path. It does not create
`results/<run>/project` and does not give agents a writable copy of the SUT.

Each benchmark item uses one unique change ID, so the filesystem itself is the
result list:

```text
<sut>/qa/changes/<benchmark-change-id>/
```

The benchmark may retain harness-only logs and timing evidence under its own
`results/<run>/`, but those files are diagnostics, not the Assurance result and
never contain another SUT copy. On achieved success the benchmark invokes
`aa export`; on failure it does not publish SUT files.

The benchmark does not register results with OpenChamber and does not write a
run-list or latest-run pointer.

## 12. OpenChamber behavior

OpenChamber lists active Assurance results by scanning:

```text
<project-root>/qa/changes/*
```

Each directory is one selectable change/run. The panel reads status, workflow
events, cases, plans, reviews, execution evidence, reports, and generated-file
manifests from that same change directory. It ignores `.staging` and ephemeral
execution views.

There is no fallback to Phase 5 `results/**/project`, `workspace/HEAD.json`, or
whole-tree exports. Missing or invalid typed artifacts remain visible as a
change error rather than causing OpenChamber to select stale data.

## 13. Archive behavior

Archive remains a separate Assurance product operation after achieved publish.
It may copy or move the canonical change record to `qa/archive/<change-id>`
according to product policy, but it does not publish generated tests;
publication belongs only to `aa export`. Calling archive before a successful
publish receipt fails closed.

The benchmark does not archive automatically. OpenChamber therefore consumes
the active result directly from `qa/changes/<id>`. No compatibility reader is
added for prior result roots.

## 14. Recovery and security

- Ledger and durable external-activity receipts remain authoritative for graph
  execution and provider recovery.
- Promotion receipts bind attempt identity and candidate digests.
- Publish receipts bind the achieved change, apply manifest, source digests,
  target baselines, and final target digests.
- Canonical paths reject absolute paths, `..`, symlinks, hard links, special
  files, and path identity drift.
- The SUT is never allowed to register executable code with graph-engine.
- Model/session state is not used as publication authority.
- Staging data alone can never advance a graph node or make a change achieved.
- A completed promotion receipt is published before cleanup; cleanup failure is
  non-terminal and replay removes authenticated pending/temp/rollback residue.
- Installed handlers are trusted runtime code. Host-v2 root authentication
  rejects persistent directory replacement and protocol path substitution, but
  does not claim confinement against a malicious trusted handler performing a
  same-permission swap-use-restore during its own call.
- Provider/model processes are untrusted. OpenCode tool mediation and the Cursor
  OS sandbox must make the project root read-only and the authenticated attempt
  `write_root` the only writable project namespace. Acceptance includes failed
  provider shell/tool attempts to rename, replace, or swap-use-restore the
  project root; this boundary is completed by implementation Tasks 5 and 6.

## 15. Migration and deletion

This is a hard replacement with no compatibility path.

Delete or retire:

- Phase 5 whole-SUT seed/materialization from the Assurance product path;
- `SnapshotStore` usage for Assurance task commits;
- `workspace/trees`, `workspace/attempts` full project copies, and
  `workspace/HEAD.json` from Assurance invocations;
- whole-tree `ResultExportV1` as the user-facing result;
- `auto_archive` in `ProductInputV1` and the archive branch inside `full`;
- benchmark `results/<run>/project` and `results/<run>/export` directories;
- OpenChamber latest-run/result-root bridge logic; and
- tests that require failed invocations to expose a HEAD snapshot.

Keep generic tree utilities only if another installed graph-engine product uses
them through a real interface. Otherwise delete them rather than preserving a
hypothetical seam.

## 16. Verification

Required tests include:

1. a failed agent write leaves original SUT and canonical change outputs
   unchanged;
2. invalid structured output leaves only non-canonical staging data, which
   cleanup removes;
3. retry promotes only the successful attempt;
4. four generation families promote concurrently without physical overwrite;
5. conflicting shared target paths fail before execution and export;
6. execution runs candidate tests without writing them into original `tests/**`;
7. non-achieved export is rejected without SUT mutation;
8. achieved export publishes exactly the manifest file set;
9. interrupted export resumes or rolls back without unlisted changes;
10. repeated export is idempotent;
11. target drift rejects export;
12. OpenChamber lists multiple `qa/changes/*` directories directly;
13. failed changes remain inspectable while failed staging output is hidden;
14. benchmark failure creates no `results/<run>/project` or whole-tree export;
15. graph-engine contains no `qa`, Assurance family, or default-graph meaning.

## 17. Acceptance criteria

The replacement is complete when:

- a live single-item OpenCode benchmark runs from the original SUT path;
- every intermediate and failed write is confined to its change;
- the original SUT changes only after terminal achieved `aa export`;
- OpenChamber displays the run directly from `qa/changes/<id>`;
- no Phase 5 whole-project copy, tree store, HEAD pointer, or full export is
  required by the Assurance workflow; and
- the graph engine remains business-neutral.
