from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
)
from tests.product.composition_harness import request_for
from tests.product.test_result_export import CHANGE_ID, write_achieved

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _reset_runtime_selector() -> None:
    yield
    try:
        from assurance_product.runtime_selection import use_test_runtime_selector

        use_test_runtime_selector(None)
    except ImportError:
        return


def test_langgraph_export_and_archive_never_call_legacy_driver(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_selection import use_test_runtime_selector
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    use_test_runtime_selector(lambda _entrypoint: "langgraph-v1")
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lg-export-001",
        entrypoint="archive",
        change_id=CHANGE_ID,
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output

    def _forbid(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("LangGraph export/archive must not call legacy Engine/driver/fold_events")
    monkeypatch.setattr("graph_engine.evidence.legacy_v2.fold_legacy_events", _forbid)
    project = write_achieved(tmp_path, project=project_dir)
    exported = cli_runner.invoke(
        app,
        ["export", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert exported.exit_code == 0, exported.output
    archived = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert archived.exit_code == 0, archived.output


def test_lock_show_renders_v2_for_legacy_and_v3_for_langgraph(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.revision_registry import RevisionRegistry
    from assurance_product.runtime_selection import (
        LegacyRuntimeRecord,
        complete_initialized,
        use_test_runtime_selector,
        write_initializing,
    )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    leftover_args, leftover_project, leftover_change = common_lifecycle_args(
        tmp_path=tmp_path / "legacy",
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lock-v2",
    )
    (leftover_project / "qa" / "changes" / leftover_change).mkdir(parents=True, exist_ok=True)
    leftover_workspace = ChangeWorkspace.open(leftover_project, leftover_change)
    leftover_workspace.initialize()
    leftover_lock_bytes = (
        Path(__file__).resolve().parents[2]
        / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
    ).read_text(encoding="utf-8").strip().encode()
    leftover_invocation = leftover_workspace.paths.runtime_root / "invocations" / "inv-lock-v2"
    leftover_invocation.mkdir(parents=True, exist_ok=True)
    (leftover_invocation / "invocation.lock.json").write_bytes(leftover_lock_bytes)
    from graph_engine.evidence.legacy_v2 import authenticate_invocation_lock_v2

    leftover_digest = authenticate_invocation_lock_v2(leftover_lock_bytes).digest
    write_initializing(
        leftover_workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id="inv-lock-v2",
            entrypoint="archive",
            root_input_digest="c" * 64,
            build_identity=leftover_digest,
        ),
    )
    complete_initialized(
        leftover_workspace,
        LegacyRuntimeRecord(
            phase="initialized",
            invocation_id="inv-lock-v2",
            entrypoint="archive",
            root_input_digest="c" * 64,
            build_identity=leftover_digest,
            identity_digest=leftover_digest,
        ),
    )
    RevisionRegistry(leftover_workspace).bind("inv-lock-v2", runtime="legacy-v2", revision_id=leftover_digest)
    leftover_lock = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            "--json",
            "--project-dir",
            str(leftover_project),
            "--change",
            leftover_change,
            "--invocation-id",
            "inv-lock-v2",
            "--product",
            leftover_args[leftover_args.index("--product") + 1],
            "--binding-dist",
            leftover_args[leftover_args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            leftover_args[leftover_args.index("--binding-declaration") + 1],
            "--config-tree",
            leftover_args[leftover_args.index("--config-tree") + 1],
            "--secret",
            leftover_args[leftover_args.index("--secret") + 1],
        ],
    )
    assert leftover_lock.exit_code == 0, leftover_lock.output
    leftover_document = parse_json_output(leftover_lock.stdout)
    assert leftover_document["lock"]["schema_version"] == "2"
    assert "compiled_workflow" in leftover_document["lock"]

    use_test_runtime_selector(lambda _entrypoint: "langgraph-v1")
    lg_args, lg_project, lg_change = common_lifecycle_args(
        tmp_path=tmp_path / "langgraph",
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lock-v3",
        entrypoint="archive",
    )
    lg_start = cli_runner.invoke(app, ["start", *lg_args])
    assert lg_start.exit_code == 0, lg_start.output
    lg_lock = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            "--json",
            "--project-dir",
            str(lg_project),
            "--change",
            lg_change,
            "--invocation-id",
            "inv-lock-v3",
            "--product",
            lg_args[lg_args.index("--product") + 1],
            "--binding-dist",
            lg_args[lg_args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            lg_args[lg_args.index("--binding-declaration") + 1],
            "--config-tree",
            lg_args[lg_args.index("--config-tree") + 1],
            "--secret",
            lg_args[lg_args.index("--secret") + 1],
        ],
    )
    assert lg_lock.exit_code == 0, lg_lock.output
    document = parse_json_output(lg_lock.stdout)
    assert document["lock"]["schema_version"] == "3"
    assert "compiled_workflow" not in document["lock"]
    assert "workflow_digest" not in document["lock"]
    assert "revision" in document
    assert document["revision"]["revision_id"]


def test_lock_show_fails_on_ambiguous_runtime_evidence(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-lock-ambiguous",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    leftover_lock = (
        project_dir
        / "qa"
        / "changes"
        / change_id
        / ".runtime"
        / "invocations"
        / "inv-lock-ambiguous"
        / "invocation.lock.json"
    )
    leftover_lock.parent.mkdir(parents=True, exist_ok=True)
    leftover_lock.write_bytes(
        (
            Path(__file__).resolve().parents[2]
            / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
        ).read_text(encoding="utf-8").strip().encode()
    )
    result = cli_runner.invoke(
        app,
        [
            "lock",
            "show",
            "--json",
            "--project-dir",
            str(project_dir),
            "--change",
            change_id,
            "--invocation-id",
            "inv-lock-ambiguous",
            "--product",
            args[args.index("--product") + 1],
            "--binding-dist",
            args[args.index("--binding-dist") + 1],
            "--binding-entrypoint",
            "deployment",
            "--binding-declaration",
            args[args.index("--binding-declaration") + 1],
            "--config-tree",
            args[args.index("--config-tree") + 1],
            "--secret",
            args[args.index("--secret") + 1],
        ],
    )
    assert result.exit_code == 40, result.output


def test_tampered_receipt_is_rejected_before_archive(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app
    from assurance_product.export import publish_achieved

    project = write_achieved(tmp_path)
    publish_achieved(project, CHANGE_ID)
    receipt = project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    files = payload["files"]
    files[0]["final_sha256"] = files[0]["final_sha256"][:-1] + (
        "0" if files[0]["final_sha256"][-1] != "0" else "1"
    )
    receipt.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = cli_runner.invoke(
        app,
        ["archive", "--json", "--project-dir", str(project), "--change", CHANGE_ID],
    )
    assert result.exit_code == 40, result.output
