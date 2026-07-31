#!/usr/bin/env bash
# scripts/packaging_smoke_test.sh
# Build a wheel, install it into a throwaway venv, and run aa outside the
# source tree to prove packaged resources resolve without the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

cd "$REPO_ROOT"
uv build --wheel --out-dir "$WORK_DIR/dist"

uv venv --python 3.11 "$WORK_DIR/venv"
uv pip install --quiet --python "$WORK_DIR/venv/bin/python" "$WORK_DIR"/dist/*.whl

mkdir "$WORK_DIR/project"
cd "$WORK_DIR/project"

"$WORK_DIR/venv/bin/aa" --version
"$WORK_DIR/venv/bin/aa" init --yes
test -f .aa/config.yaml
test -f .aa/execution-policy.json
"$WORK_DIR/venv/bin/aa" doctor --json > doctor.json
"$WORK_DIR/venv/bin/python" - <<'PY'
import json
doc = json.load(open("doctor.json"))
assert doc["status"] in ("ok", "warning"), doc["status"]
PY
"$WORK_DIR/venv/bin/aa" config print > /dev/null

# Packaged resources must be readable from the wheel install.
"$WORK_DIR/venv/bin/python" - <<'PY'
from assurance_agent import resources

workflow = resources.read_text("schemas", "workflow-schema.yaml")
assert "aa-full" in workflow
assert "manual_revision:" in workflow
assert "skill:aa-fuzz-plan" in workflow
assert "skill:aa-performance-plan" in workflow

contracts = resources.read_text("schemas", "execution-contracts.yaml")
for target in (
    "skill:aa-fuzz-plan:",
    "skill:aa-fuzz-plan-reviewer:",
    "skill:aa-fuzz-codegen:",
    "skill:aa-performance-plan:",
    "skill:aa-performance-plan-reviewer:",
    "skill:aa-performance-codegen:",
):
    assert target in contracts, target
PY

# New production modules must import from the wheel (not only the repo tree).
"$WORK_DIR/venv/bin/python" - <<'PY'
import assurance_agent.artifacts.policy_obligations as policy_obligations
import assurance_agent.workflow.graph.manual_revision as manual_revision

assert policy_obligations.DEFERRED_POLICY_OBLIGATIONS
assert hasattr(manual_revision, "ManualRevisionError")
PY

# Packaged skills + opencode assets must resolve from the wheel install.
"$WORK_DIR/venv/bin/python" - <<'PY'
from assurance_agent import resources
skills = resources.iter_children("skills")
assert len(skills) == 38, f"expected 38 skills, got {len(skills)}"
required = {
    "aa-workflow",
    "writing-skills",
    "aa-improvement-reviewer",
    "aa-retro-issue-analysis",
    "aa-retro-workflow-analysis",
    "aa-retro-eval-analysis",
    "aa-fuzz-plan",
    "aa-fuzz-plan-reviewer",
    "aa-fuzz-codegen",
    "aa-performance-plan",
    "aa-performance-plan-reviewer",
    "aa-performance-codegen",
}
assert required <= set(skills), sorted(required - set(skills))
assert "aa-doc-author.md" in resources.iter_children("opencode", "agents")
assert "aa.mjs" in resources.iter_children("opencode", "plugins")
assert "workflow_start.ts" in resources.iter_children("opencode", "tools")
PY

# The one-off migration dev tool must NOT ship in the wheel.
"$WORK_DIR/venv/bin/python" - <<'PY'
import importlib.util
assert importlib.util.find_spec("scripts") is None, "scripts package leaked into wheel"
assert importlib.util.find_spec("migrate_skills") is None, "migrate_skills leaked into wheel"
PY

echo "packaging smoke test: OK"
