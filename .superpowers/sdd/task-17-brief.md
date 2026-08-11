### Task 17: Replace Porcelain Authority with Content Manifests and Export the Root Evidence Closure

**Files:**
- Create: `assurance_agent/eval/evidence_export.py`
- Modify: `assurance_agent/eval/change_location_evidence.py`
- Modify: `assurance_agent/eval/write_scan.py`
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/scorers/shared.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Modify: `tests/unit/eval/test_write_scan.py`
- Modify: `tests/unit/eval/test_change_location_evidence.py`
- Create: `tests/unit/eval/test_evidence_export.py`
- Modify: `tests/unit/eval/test_executor.py`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_task_input_snapshot.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/workflow/graph/test_runtime_commit_safety.py`

**Interfaces:**
- Produces: `WorktreeManifestEntryV1`, `WorktreeManifestV1`, `WriteDiffEntryV1`, `WriteDiffV1`, `WritePolicyV1`, strict `ExecutionEvidenceV1`, content capture/diff/replay APIs, `RootEventSliceEventV1`, `RootEventSliceV1`, `EvidenceExportObjectV1`, `EvidenceExportManifestV1`, and export/replay functions.
- Consumes: canonical selected layers, D17 resolved current-change path/evidence, full source ledger read once, root invocation ID, root/descendant definitions/events, snapshots, contexts, receipts, write sets, blobs, and semantics objects.
- Preserves: porcelain files for diagnostics and `_copy_sut_tests` for human inspection; neither earns authority or current-write credit.

- [ ] **Step 1: Add strict fail-closed content-manifest tests**

  Traverse directories but serialize only regular-file and symlink leaves; bind normalized relative path, kind, mode, size, content digest or symlink target using `lstat` without following. Exclude exactly `.git/**` and the external attempt/evidence directory. Require canonical UTF-8 byte ordering plus exact aggregate entry/file-byte counts. Reject escape, duplicate path, extra/coerced fields, inconsistent kind fields, socket/FIFO/device leaves, permission/read/stat failure, identity/kind race between stat and read, more than 250,000 entries, or more than 4 GiB hashed bytes.
- [ ] **Step 2: Add full content-diff coverage**

  Detect add/delete, byte modify, chmod, file-to-symlink, symlink-target change, clean tracked, pre-dirty, untracked, and ignored paths. An unchanged pre-dirty path must not be attributed. Preserve before/after evidence even when porcelain text is identical or omits an ignored file. Replay must recompute the complete canonical diff from both manifests and require byte-exact equality with `write-diff.json`; forged-empty, omitted, extra, reordered, or wrong-reason entries fail evidence integrity before policy scoring.
- [ ] **Step 3: Add exact selected-layer policy and graph-authority parity tests**

  For all fifteen selections and reverse-order inputs, require canonical bytes allowing only current change, graph locks/publications, `tests/testdata` and selected private roots. Reject all-changes, sibling tests, product code, memory, arbitrary runtime metadata, another change, archive root, and `eval/out/runs/**` inside the attempt SUT. For each subset, derive the selected execution-contract write-claim union and prove `WritePolicyV1` makes the same selected/sibling/shared decisions; any drift fails the shared parity test.
- [ ] **Step 4: Add the root event-slice model tests**

  Use two interleaved roots in one valid ledger. Require `export_seq` exactly `1..N`, strictly increasing `source_seq` with permitted gaps, production graph-event validation, exact root descendant closure, and optional one exact D18 supersede event only when its authorization is consumed by this root.
- [ ] **Step 5: Add transitive object-closure and D15 root-map tests**

  For v6, export exact root/child definition bindings, individual graph/contracts/catalog/policy/profile/three semantics objects, selected codegen snapshots/contexts/receipts/write sets, the receipt-referenced canonical validation-decision payload as a digest-bound `blob`, and blobs for bound plan/case/hard outputs/add-modify files. For v1-v5, export only object kinds actually pinned by that historical schema; when a v4/v5 compatibility receipt is present, additionally export its exact staged audit-semantics object. Never reconstruct a missing historical object from the current executable. Verify every current write set carries `base_tree_roots`, that the map matches `base_tree_id` and participates in `write_set_id`, and that offline `project:`/`repo:` alias resolution uses only that exported map. Reject missing/tampered/extra decision blobs or root maps, unreferenced objects, duplicate identity, path escape, digest/size mismatch, ancestry escape, deleted/reordered events, wrong source metadata, or an unrelated sentinel object copied from the store.
- [ ] **Step 5a: Add strict execution-envelope tests**

  Require `selected_layers`, `selection_normalizer_version`, `write_policy_schema_version`, change ID, safely resolved repository-relative active-change root, and root invocation ID for both fresh and import-checkpoint execution. A failure before root establishment records null and receives no current credit. Bind the copied config/change-location evidence, both `write-manifest-*.json` files, diff, policy, source-ledger metadata, root slice, and export manifest by path/digest/size; reject extra/coerced/missing fields and any envelope/object disagreement.
- [ ] **Step 6: Run tests and observe porcelain/wide-copy limitations**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  ```

  Expected: ignored/content/kind mutations are invisible and no bounded runtime evidence closure exists.
- [ ] **Step 7: Implement content capture, D17 persistence, diff, and strict policy**

  Capture `write-manifest-before.json` independently after fixture seeding and before runtime. Copy the exact config, build/replay D17 evidence from that manifest, then construct policy from the selected tuple and active change. On unsafe/archive-only resolution, persist infrastructure evidence but no writable policy or root. Capture `write-manifest-after.json` regardless of runtime result; persist canonical files and bind every digest/size plus both schema-version fields in strict `execution.json`. Compute forbidden writes only from validated `WriteDiffV1` plus `WritePolicyV1`.
- [ ] **Step 8: Implement one-pass root evidence export**

  Read and digest the full source ledger once, select closure by parent invocation lineage, then traverse the schema-version-specific explicit object references. Do not recursively export trees. Export each current receipt's canonical decision bytes and each current write set's bound `base_tree_roots`; verify their digests before recording them. Preserve historical object sparsity and receipt-bound audit semantics exactly. Preserve the exact source metadata/pairs in `execution.json`; failure before a root leaves root ID null and cannot receive current credit. Extend the D13 and D10/D14 field-consumer inventories with snapshot/context, receipt-decision, D15 root-map, and all three current semantics object/digest export reads.
- [ ] **Step 9: Make shared evidence integrity fail closed**

  Require strict execution, D17, manifests/diff/policy, root slice, export manifest, and every referenced object. Recompute the full diff from the two bound manifests and compare canonical bytes rather than trusting a persisted count/path list; a forged empty diff over a forbidden manifest change is integrity failure. Missing write evidence or object bytes scores integrity zero; never synthesize forbidden count zero from absence.
- [ ] **Step 10: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  uv run ruff check assurance_agent/eval/write_scan.py assurance_agent/eval/change_location_evidence.py assurance_agent/eval/evidence_export.py assurance_agent/eval/executor.py assurance_agent/eval/scorers/shared.py tests/unit/eval
  uv run pyright
  ```

  Expected: every manifest/export mutation fails at evidence integrity and all policy subsets produce stable bytes.
- [ ] **Step 11: Commit content-aware bounded evidence**

  ```bash
  git add assurance_agent/eval/evidence_export.py \
    assurance_agent/eval/change_location_evidence.py \
    assurance_agent/eval/write_scan.py \
    assurance_agent/eval/executor.py \
    assurance_agent/eval/scorers/shared.py \
    assurance_agent/workflow/graph/workspace.py \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  git commit -m "feat(eval): export content-bound runtime evidence"
  ```

