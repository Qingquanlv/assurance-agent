"""Named files that carry data between retro and apply steps."""

from __future__ import annotations

from graph_engine.stategraph.ledger import NamedWrite

RETRO_DIR = "qa/results/retro"
IMPROVEMENT_DIR = "qa/results/improvement"

ISSUE_SLICE = f"{RETRO_DIR}/issue-slice.json"
WORKFLOW_SLICE = f"{RETRO_DIR}/workflow-slice.json"
EVAL_SLICE = f"{RETRO_DIR}/eval-slice.json"
DISCOVERY_SLICE = f"{RETRO_DIR}/discovery-slice.json"
COVERAGE_GAP_SLICE = f"{RETRO_DIR}/coverage-gap-slice.json"
COLLECTED = f"{RETRO_DIR}/collected.json"
CONTEXT = f"{RETRO_DIR}/context.json"
CANDIDATES = f"{RETRO_DIR}/candidates.json"
PROJECTION = f"{IMPROVEMENT_DIR}/projection.json"
MEMORY_EVAL = f"{IMPROVEMENT_DIR}/memory-eval.json"

SLICE_WRITES: tuple[NamedWrite, ...] = (
    NamedWrite("issue", ISSUE_SLICE),
    NamedWrite("workflow", WORKFLOW_SLICE),
    NamedWrite("eval", EVAL_SLICE),
    NamedWrite("discovery", DISCOVERY_SLICE),
    NamedWrite("coverage_gap", COVERAGE_GAP_SLICE),
)
COLLECTED_WRITE = NamedWrite("collected", COLLECTED)
CONTEXT_WRITE = NamedWrite("context", CONTEXT)
CANDIDATES_WRITE = NamedWrite("candidates", CANDIDATES)
PROJECTION_WRITE = NamedWrite("projection", PROJECTION)
MEMORY_EVAL_WRITE = NamedWrite("memory-eval", MEMORY_EVAL)

SLICE_PATH = {
    "issue": ISSUE_SLICE,
    "workflow": WORKFLOW_SLICE,
    "eval": EVAL_SLICE,
    "discovery": DISCOVERY_SLICE,
    "coverage_gap": COVERAGE_GAP_SLICE,
}
