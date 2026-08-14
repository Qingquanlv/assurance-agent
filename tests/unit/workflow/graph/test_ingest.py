"""Blob 摄入（ingest_from_write_set）的 codec 处理回归。

回归点：registry-fallback 分支曾对所有 must_compat artifact 无条件 ``json.loads``，
导致 ``.qa.yaml`` 这类 YAML must_compat 产物被当成 JSON 解析，报出
"Expecting value: line 1 column 1 (char 0)" 并让 case-design 节点 3/3 失败。
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.generated_files import ApiGeneratedFilesV1
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.ingest import ingest_from_write_set
from assurance_agent.workflow.graph.ingest_catalog import resolve_model
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend

_QA_YAML = """\
schema_version: "1.0"
schema: case-driven
created_at: "2026-07-24T09:56:14.000Z"
change:
  change_id: CH-1
  requirement_id: RET-x
  feature_name: api-management
  status: draft
targets:
  cases: []
approval:
  mode: autonomous
  approved_by: aa-workflow
  approved_approach: API
  approved_at: "2026-07-24T09:56:14.000Z"
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return project


def _claims(*patterns: str) -> ResourceClaims:
    parsed = tuple(ResourcePath.parse(p) for p in patterns)
    return ResourceClaims(writes=parsed, authorization_writes=parsed)


def test_ingest_reads_yaml_must_compat_artifact_via_registry_fallback(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = TreeStore(project / "qa" / "changes" / "CH-1")
    backend = WorkspaceBackend(project / "qa" / "changes" / "CH-1")
    workspace = backend.create(task_id="task-a", base_tree_id=store.capture(project), store=store)

    (workspace.change_dir / ".qa.yaml").write_text(_QA_YAML, encoding="utf-8")
    write_set = store.freeze_write_set(
        workspace,
        claims=_claims("change:.qa.yaml"),
        outputs=("change:.qa.yaml",),
    )

    frozen = ingest_from_write_set(
        store,
        write_set_id=write_set.write_set_id,
        output_paths=("change:.qa.yaml",),
    )

    assert "_qa_yaml" in frozen
    value = frozen["_qa_yaml"].value
    assert isinstance(value, dict)
    assert value["schema_version"] == "1.0"
    change = value["change"]
    assert isinstance(change, dict)
    assert change["change_id"] == "CH-1"
    assert frozen["_qa_yaml"].model_id == "qa_yaml"
    # The frozen value must stay faithful to the raw document so the attached-gate
    # dual run (candidate override vs disk parse) yields the same verdict.  In
    # particular, approval is gate-consumed metadata and schema is an aliased field.
    approval = value["approval"]
    assert isinstance(approval, dict)
    assert approval["mode"] == "autonomous"
    assert value["schema"] == "case-driven"
    assert "schema_" not in value


def test_resolve_model_knows_dormant_generated_files_model_ids() -> None:
    assert resolve_model("api_generated_files/v1") is ApiGeneratedFilesV1
    try:
        resolve_model("not_a_model@1")
    except ValueError as exc:
        assert "unknown ingest model id" in str(exc)
    else:
        raise AssertionError("expected ValueError")
