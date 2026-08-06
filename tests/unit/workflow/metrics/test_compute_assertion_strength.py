"""M2 Task 4: compute-assertion-strength reuses M1 assertion_class classifiers."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from assurance_agent.artifacts.models.metrics import PR_METRICS_REL
from assurance_agent.artifacts.models.pr_metric_evidence import AssertionStrengthEvidence
from assurance_agent.verification import assertion_class as assertion_class_mod
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.assertion_strength import (
    ASSERTION_STRENGTH_BATCH_ID,
    ASSERTION_STRENGTH_EVIDENCE_REL,
    compute_assertion_strength,
    compute_assertion_strength_operation,
)
from assurance_agent.workflow.metrics.nightly import (
    ASSERTION_STRENGTH_EVIDENCE_REL as NIGHTLY_ASSERTION_REL,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-ASRT-001"

_API_STRONG = (
    "def test_strong(client):\n"
    "    response = client.post('/depts', json={'name': 'dup'})\n"
    "    assert response.status_code == 400\n"
    "    assert response.json()['detail'] == 'name already exists'\n"
)
_API_WEAK = (
    "def test_weak(client):\n    response = client.get('/menus')\n    assert response.status_code == 200\n"
)
_E2E_STRONG = (
    "async def test_strong(page):\n"
    "    await page.goto('/depts')\n"
    "    await expect(page.get_by_text('Engineering')).to_have_text('Engineering')\n"
)
_E2E_WEAK = (
    "async def test_weak(page):\n"
    "    await page.goto('/login')\n"
    "    await expect(page.get_by_role('button')).to_be_visible()\n"
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write_aa_config(root)
    change = root / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    return root


def _write_tests(root: Path, *, api: dict[str, str] | None = None, e2e: dict[str, str] | None = None) -> None:
    if api is not None:
        api_dir = root / "tests" / "api"
        api_dir.mkdir(parents=True, exist_ok=True)
        for name, source in api.items():
            (api_dir / name).write_text(source, encoding="utf-8")
    if e2e is not None:
        e2e_dir = root / "tests" / "e2e"
        e2e_dir.mkdir(parents=True, exist_ok=True)
        for name, source in e2e.items():
            (e2e_dir / name).write_text(source, encoding="utf-8")


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t-asrt",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-asrt",
        node_id="compute-assertion-strength",
        graph_id="metrics-nightly-workflow",
        target="operation:compute-assertion-strength",
        input={"with": {}},
    )


def test_evidence_path_matches_nightly_contract_surface() -> None:
    assert ASSERTION_STRENGTH_EVIDENCE_REL == NIGHTLY_ASSERTION_REL
    assert ASSERTION_STRENGTH_EVIDENCE_REL == "execution/runs/nightly/assertion-strength.json"
    assert ASSERTION_STRENGTH_BATCH_ID == "nightly"


def test_both_surfaces_evaluated_with_counts_and_weak_locators(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_tests(
        root,
        api={"test_api.py": _API_STRONG + "\n" + _API_WEAK},
        e2e={"test_e2e.py": _E2E_STRONG + "\n" + _E2E_WEAK},
    )
    evidence = compute_assertion_strength(project_root=root, change_id=CHANGE_ID)
    assert evidence.status == "evaluated"
    assert evidence.batch_id == "nightly"
    assert evidence.declared is not None
    assert evidence.declared.total == 4
    assert evidence.declared.covered == 2
    assert evidence.value == 0.5
    by_layer = {s.layer: s for s in evidence.surfaces}
    assert set(by_layer) == {"api", "e2e"}
    assert by_layer["api"].declared.total == 2
    assert by_layer["api"].strong == 1
    assert by_layer["api"].weak == 1
    assert by_layer["e2e"].declared.total == 2
    assert by_layer["e2e"].strong == 1
    weak_locators = {w.locator for w in evidence.weak_assertions}
    assert "tests/api/test_api.py::test_weak" in weak_locators
    assert "tests/e2e/test_e2e.py::test_weak" in weak_locators
    api_weak = next(w for w in evidence.weak_assertions if w.surface == "api")
    assert "status_200_only" in api_weak.reasons


def test_missing_surface_is_not_evaluated_with_pending_nightly(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_tests(root, api={"test_api.py": _API_STRONG}, e2e=None)
    evidence = compute_assertion_strength(project_root=root, change_id=CHANGE_ID)
    assert evidence.status == "not_evaluated"
    assert evidence.value is None
    assert evidence.surfaces == ()
    assert any(b.code == "pending_nightly" and b.metric == "assertion_strength" for b in evidence.shortboards)


def test_empty_trees_are_not_evaluated_pending_nightly(tmp_path: Path) -> None:
    root = _project(tmp_path)
    evidence = compute_assertion_strength(project_root=root, change_id=CHANGE_ID)
    assert evidence.status == "not_evaluated"
    assert any(b.code == "pending_nightly" for b in evidence.shortboards)


def test_operation_writes_evidence_and_never_pr_metrics(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_tests(
        root,
        api={"test_api.py": _API_STRONG},
        e2e={"test_e2e.py": _E2E_STRONG},
    )
    workspace = _workspace(root)
    pr = workspace.change_dir / PR_METRICS_REL
    pr.parent.mkdir(parents=True, exist_ok=True)
    planted = b'{"planted":true}\n'
    pr.write_bytes(planted)

    result = compute_assertion_strength_operation(_task(), workspace, _context(root))
    assert result.status == "succeeded"
    assert pr.read_bytes() == planted
    path = workspace.change_dir / ASSERTION_STRENGTH_EVIDENCE_REL
    assert path.is_file()
    payload = AssertionStrengthEvidence.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert payload.status == "evaluated"
    assert payload.change_id == CHANGE_ID
    text = path.read_text(encoding="utf-8")
    assert "stub pending M2 Task 4" not in text


def test_reuses_m1_classifier_module_without_second_taxonomy(tmp_path: Path, monkeypatch) -> None:
    """Collector must call assertion_class; must not define a parallel AST taxonomy."""
    root = _project(tmp_path)
    _write_tests(
        root,
        api={"test_api.py": _API_WEAK},
        e2e={"test_e2e.py": _E2E_WEAK},
    )
    calls: list[tuple[str, str | None]] = []
    real_api = assertion_class_mod.classify_api_assertions
    real_e2e = assertion_class_mod.classify_e2e_assertions

    def wrap_api(source: str, *, function_name: str | None = None):
        calls.append(("api", function_name))
        return real_api(source, function_name=function_name)

    def wrap_e2e(source: str, *, function_name: str | None = None):
        calls.append(("e2e", function_name))
        return real_e2e(source, function_name=function_name)

    monkeypatch.setattr(assertion_class_mod, "classify_api_assertions", wrap_api)
    monkeypatch.setattr(assertion_class_mod, "classify_e2e_assertions", wrap_e2e)
    # Import after monkeypatch path used by the collector module.
    import assurance_agent.workflow.metrics.assertion_strength as mod

    monkeypatch.setattr(mod, "classify_api_assertions", wrap_api)
    monkeypatch.setattr(mod, "classify_e2e_assertions", wrap_e2e)

    evidence = compute_assertion_strength(project_root=root, change_id=CHANGE_ID)
    assert evidence.status == "evaluated"
    assert ("api", "test_weak") in calls
    assert ("e2e", "test_weak") in calls

    collector_src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(collector_src)
    # No second taxonomy: collector must not re-implement strength classification.
    forbidden = {"status_200_only", "visibility_only", "business_predicate", "helper_only"}
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (literals & forbidden)
