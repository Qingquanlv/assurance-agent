# Retro Workflow Analysis

Analyze only locked evidence; do not write Ledgers, snapshots, or delivery
instructions, and do not infer state from prior conversation.

Capability-owned retro-workflow-analysis skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Analyze a frozen workflow evidence slice. Schema truth is `assurance_improvement.contracts`
for `RetroAnalysisResultV3` with `domain=workflow`.

## Inputs

### required

- `change_id` and the authenticated `evidence_slice` supplied in the JSON input
- the slice includes `retro_id`, `domain`, `window`, `entries`, `sources`, integrity, and deterministic signals
- cite only source evidence IDs present in this slice; empty entries are not permission to invent evidence

## Outputs

### required

- `candidates: []`: domain analysis produces signals only
- structured `RetroAnalysisResultV3` with `domain` `workflow`
- `analysis_status` `ok` or `failed`
- signals cite only `workflow_evidence_ids` present in the authenticated sources

## Rules

- Treat deterministic slice signals as already included. Analyze entries for
  additional patterns. Do not copy or re-emit a deterministic signal.
- Group evidence by typed identity:
  - `gate_verdict`: group by `gate_id + cause`; emit `gate_pushback`
  - `task_failure`: group by `node_id + error_kind + message_fingerprint`
  - `healing_outcome`: group by `operation + outcome`; emit `healing`
  - `skill_drift`: group by `phase + expected_skill`; emit `skill_drift`
  - `loop_round`: group by `change_id + coverage_epoch + loop_kind + family` and
    order by `round_index`. A `needs_fix`, `rework`, `rejected`, or `exhausted`
    round is recorded review/repair friction even when followed by `pass` or
    `approved`. Emit one `gate_pushback` per actionable group, with `gate_id`
    formed from `loop_kind` and optional `family`, and cause `review_rework`.
    Cite the actual round evidence IDs; count adverse rounds, not successful rounds
    or technical attempts. All-success rounds alone do not establish a problem.
- A recovered `task_failure` still happened. Do not discard it because a later
  review passed; deterministic task-failure signals already preserve these events.
- `loop_round` or technical failure alone cannot establish `skill_drift`. Require
  a typed `skill_drift` entry; missing drift evidence is an integrity gap, not proof
  of either compliance or deviation. Do not derive drift from report prose.
- Analyze usable entries even when slice integrity is incomplete. An empty signal
  list means no additional supported pattern, not that missing evidence was checked.
- Each `signal_id` may occur only once.
- On failure write `analysis_status: failed`, a non-empty `failure_reason`, and no signals.
- Never write `slice_sha256` or calculate a digest.
- Do not emit provider session transcripts or secret-bearing diagnostics.
- Use the locked execution binding from the prepare request.
- Write the typed result to `qa/results/retro/retro-workflow-analysis.json`.
- Write and return the same complete `RetroAnalysisResultV3` JSON object. No Markdown fence or prose in the final answer.
- Return the typed result and stop.
