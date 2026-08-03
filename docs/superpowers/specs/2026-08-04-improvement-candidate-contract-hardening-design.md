# Improvement Candidate Contract Hardening Design

## Problem

Retro accepted two semantically invalid Improvement candidates:

- a `memory_patch` targeted `skills/awe-api-plan:required-field-summary-probe`, although memory delivery can only write beneath `.aa/memory/`;
- a knowledge delta represented the concrete ORM constraint `max_length=20` as `max_length: true`.

Both candidates passed structural parsing because `target` is only required to be non-empty and entity constraints are currently `dict[str, Any]`. Delivery would reject the first candidate late, while the second could publish imprecise knowledge after human approval.

## Design

### Memory-patch targets

Add one pure, workspace-independent target predicate in the Improvement artifact layer. A valid `memory_patch` target must:

- be a relative POSIX path;
- be strictly below `.aa/memory/`;
- contain no `.` or `..` components and no backslashes;
- identify a child path rather than the `.aa/memory` directory itself.

`ImprovementCandidate` will reject incompatible targets during candidate-document parsing. Auto-review will reuse the same predicate when calculating `delivery_allowed`, so legacy invalid projections fail closed instead of reporting delivery as allowed. The existing filesystem-aware apply guard remains the final symlink and containment check.

Targets for `change_draft` and `knowledge_delta` remain logical identifiers and are unaffected.

### Knowledge constraint values

Keep `EntityLeaf.constraints` extensible and compatible with existing flattened booleans such as `name_has_max_length: true`. Add recursive validation for the semantic key `max_length`: its value must be a positive integer and must not be a boolean.

This permits precise nested knowledge such as:

```yaml
constraints:
  name:
    max_length: 20
    unique: true
```

and rejects `max_length: true`. No existing L1 migration is required.

## Error handling

Invalid new candidates fail as `CandidateBatchInvalid` through the existing Pydantic error mapping, preserving whole-batch zero-write behavior. Legacy invalid Improvement projections remain readable, but Auto-review marks their delivery ineligible and apply retains its existing hard rejection.

## Verification

Tests will cover:

- valid and invalid `memory_patch` target forms at the candidate-model seam;
- Auto-review `delivery_allowed=false` for a legacy invalid projection;
- compatibility with existing flattened constraint flags;
- rejection of boolean, zero, negative, and string `max_length` values;
- acceptance of a positive integer `max_length`;
- candidate-document validation for both original failure shapes.

Relevant unit suites, Ruff, Pyright, and the original artifact replay check must pass before completion.

## Non-goals

- Migrating all existing constraint keys to a closed schema.
- Rewriting or deleting historical Improvement ledger events.
- Changing approval policy for medium-risk or domain-knowledge Improvements.
