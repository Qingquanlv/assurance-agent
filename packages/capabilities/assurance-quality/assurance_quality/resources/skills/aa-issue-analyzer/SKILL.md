# Issue analyzer

Capability-owned issue-analyzer skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Read immutable Observations and allowlisted evidence, then propose Issue Candidates.
Schema truth is `assurance_quality.contracts` for `IssueAnalysisResultV1`.

## Inputs

### required

- locked observations and evidence-bundle digest
- owned evidence identifiers
- optional Problem projection for semantic matches

## Outputs

### required

- structured `IssueAnalysisResultV1`
- each candidate cites at least one owned `observation_id`
- `classification` and `severity` are proposals, not canonical state
- `fingerprint_inputs` use stable surface and symptom tokens
- For `affected_surface.kind=endpoint`, `value` must contain the evidence-backed HTTP method and path, such as `POST /api/v1/dept/create`, never a bare path. Do not guess a missing method; use an evidence-backed module surface when no endpoint identity is established.
- Fingerprint surface, symptom, and qualifiers must contain meaningful tokens, not only whitespace or separators (`-`, `.`, `/`).
- `possible_problem_ids` contains only real Problem ids, or is empty

## Rules

- Do not invent Observations or evidence.
- Do not write Ledgers, snapshots, or project Issue state.
- Do not set `status`, `version`, `authority`, `resolved`, or `merged` on a candidate.
- Treat `review_finding` as advisory unless later allowlisted evidence still confirms it.
- Reuse an existing Problem fingerprint preimage exactly when it is the same Problem.
- Do not include raw secrets in hypotheses.
- The runtime stamps `candidate_digest` after validation.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/changes/<change-id>/inspect/issue-analysis.json`.
- Return the typed result and stop.
