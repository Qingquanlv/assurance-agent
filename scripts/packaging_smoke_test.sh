#!/usr/bin/env bash
# scripts/packaging_smoke_test.sh
# Build wheels from the committed tree, then prove:
#   - kernel + sample can compile without assurance-agent
#   - assurance-agent still ships aa and the four-layer graph
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Build the committed tree, not the caller's dirty worktree.  Hatch includes
# untracked Python files below assurance_agent/, which can otherwise make a
# locally green wheel depend on files a clean clone will never receive.
mkdir "$WORK_DIR/source"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK_DIR/source"

cd "$WORK_DIR/source"
uv build --wheel --all-packages --out-dir "$WORK_DIR/dist"

KERNEL_WHL="$(echo "$WORK_DIR"/dist/assurance_kernel-*.whl)"
SAMPLE_WHL="$(echo "$WORK_DIR"/dist/aa_sample_product-*.whl)"
AGENT_WHL="$(echo "$WORK_DIR"/dist/assurance_agent-*.whl)"
test -f "$KERNEL_WHL"
test -f "$SAMPLE_WHL"
test -f "$AGENT_WHL"

# 4. Kernel namelist contains policy-default.yaml; no skills/; no four-layer schema.
# 5. Assurance namelist contains workflow-schema.yaml; no policy-default.yaml.
python3 - "$KERNEL_WHL" "$AGENT_WHL" <<'PY'
import sys
import zipfile

kernel_names = zipfile.ZipFile(sys.argv[1]).namelist()
agent_names = zipfile.ZipFile(sys.argv[2]).namelist()
kernel = "\n".join(kernel_names)
agent = "\n".join(agent_names)

assert any(name.endswith("policy-default.yaml") for name in kernel_names), kernel_names
assert not any("/skills/" in name or name.endswith("/skills") for name in kernel_names), kernel
assert "workflow-schema.yaml" not in kernel, kernel

assert any(name.endswith("workflow-schema.yaml") for name in agent_names), agent_names
assert not any(name.endswith("policy-default.yaml") for name in agent_names), agent
PY

# Venv A: kernel + sample only.
uv venv --python 3.11 "$WORK_DIR/venv-a"
uv pip install --quiet --python "$WORK_DIR/venv-a/bin/python" --find-links "$WORK_DIR/dist" "$KERNEL_WHL" "$SAMPLE_WHL"
if [ -x "$WORK_DIR/venv-a/bin/aa" ]; then
  echo "aa must be absent in the kernel+sample venv" >&2
  exit 1
fi
mkdir "$WORK_DIR/project-a"
cd "$WORK_DIR/project-a"
"$WORK_DIR/venv-a/bin/python" -m assurance_kernel --product sample compile
if "$WORK_DIR/venv-a/bin/python" -m assurance_kernel compile >"$WORK_DIR/bare-compile.out" 2>&1; then
  echo "bare python -m assurance_kernel compile must exit non-zero" >&2
  cat "$WORK_DIR/bare-compile.out" >&2
  exit 1
fi
grep -qi product "$WORK_DIR/bare-compile.out"

# Venv B: assurance-agent (pulls kernel from the local dist).
uv venv --python 3.11 "$WORK_DIR/venv-b"
uv pip install --quiet --python "$WORK_DIR/venv-b/bin/python" --find-links "$WORK_DIR/dist" "$AGENT_WHL"

# Installing assurance-agent must not install or import Phase 3 adapter/fixture wheels.
python3 - "$AGENT_WHL" <<'PY'
import sys
import zipfile
from email.parser import BytesParser

with zipfile.ZipFile(sys.argv[1]) as archive:
    metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
    metadata = BytesParser().parsebytes(archive.read(metadata_name))
requirements = [
    value.lower().replace("_", "-")
    for value in metadata.get_all("Requires-Dist", [])
]
forbidden = ("agent-runtime-opencode", "agent-runtime-cursor", "agent-runtime-fixture")
assert not any(item.startswith(forbidden) for item in requirements), requirements
PY
"$WORK_DIR/venv-b/bin/python" - <<'PY'
import importlib.util
import sys
from importlib import metadata

for package in (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "agent_runtime_fixture",
):
    assert importlib.util.find_spec(package) is None, package
    assert package not in sys.modules, package

installed = {dist.metadata["Name"].replace("_", "-").lower() for dist in metadata.distributions()}
for name in (
    "agent-runtime-opencode",
    "agent-runtime-cursor",
    "agent-runtime-fixture",
):
    assert name not in installed, name
PY

mkdir "$WORK_DIR/project"
cd "$WORK_DIR/project"

"$WORK_DIR/venv-b/bin/aa" --version
"$WORK_DIR/venv-b/bin/aa" init --yes
test -f .aa/config.yaml
test -f .aa/execution-policy.json
"$WORK_DIR/venv-b/bin/aa" doctor --json > doctor.json
"$WORK_DIR/venv-b/bin/python" - <<'PY'
import json
doc = json.load(open("doctor.json"))
assert doc["status"] in ("ok", "warning"), doc["status"]
PY
"$WORK_DIR/venv-b/bin/aa" config print > /dev/null

"$WORK_DIR/venv-b/bin/aa" workflow compile --json > compile.json
grep -q '"full"' compile.json

# Packaged resources must be readable from the wheel install.
"$WORK_DIR/venv-b/bin/python" - <<'PY'
from assurance_agent.product import select_product
from assurance_agent import resources

select_product("assurance")
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
"$WORK_DIR/venv-b/bin/python" - <<'PY'
import assurance_agent.artifacts.policy_obligations as policy_obligations
import assurance_agent.verification.plan_checks as plan_checks
import assurance_agent.workflow.graph.manual_revision as manual_revision
import assurance_agent.workflow.graph.reviewer_plan_checks as reviewer_plan_checks

assert policy_obligations.DEFERRED_POLICY_OBLIGATIONS
assert hasattr(manual_revision, "ManualRevisionError")
assert hasattr(plan_checks, "run_layer_plan_checks")
assert hasattr(reviewer_plan_checks, "complete_reviewer_plan_checks")
PY

# Import the runtime registry from the installed wheel.  Schema nodes may be
# syntactically valid while their registered operation modules are absent from
# the committed package, so resource-only checks are not sufficient.
"$WORK_DIR/venv-b/bin/python" - <<'PY'
from assurance_agent.workflow.driver.operations_catalog import default_operations

assert default_operations()
PY

# Packaged skills + opencode assets must resolve from the wheel install.
"$WORK_DIR/venv-b/bin/python" - <<'PY'
from assurance_agent.product import select_product
from assurance_agent import resources

select_product("assurance")
skills = resources.iter_children("skills")
assert len(skills) == 36, f"expected 36 skills, got {len(skills)}"
required = {
    "aa-workflow",
    "writing-skills",
    "aa-improvement-reviewer",
    "aa-retro-issue-analysis",
    "aa-retro-workflow-analysis",
    "aa-retro-eval-analysis",
    "aa-coverage-repair",
    "aa-fuzz-plan",
    "aa-fuzz-plan-reviewer",
    "aa-fuzz-codegen",
    "aa-performance-plan",
    "aa-performance-plan-reviewer",
    "aa-performance-codegen",
}
assert required <= set(skills), sorted(required - set(skills))
removed = {"aa-api-plan-fixer", "aa-e2e-plan-fixer", "aa-case-fixer"}
assert not (removed & set(skills)), sorted(removed & set(skills))
assert "aa-doc-author.md" in resources.iter_children("opencode", "agents")
assert "aa.mjs" in resources.iter_children("opencode", "plugins")
assert "workflow_start.ts" in resources.iter_children("opencode", "tools")
PY

# The one-off migration dev tool must NOT ship in the wheel.
"$WORK_DIR/venv-b/bin/python" - <<'PY'
import importlib.util
assert importlib.util.find_spec("scripts") is None, "scripts package leaked into wheel"
assert importlib.util.find_spec("migrate_skills") is None, "migrate_skills leaked into wheel"
PY

"$WORK_DIR/venv-b/bin/python" - <<'PY'
import importlib.util

for name in (
    "assurance_agent.eval.specialty_models",
    "assurance_agent.eval.specialty_render",
    "assurance_agent.eval.specialty_replay",
):
    assert importlib.util.find_spec(name) is None, name
PY

echo "packaging smoke test: OK"
