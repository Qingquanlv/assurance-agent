from __future__ import annotations

import json
from pathlib import Path

from tests.product.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    source_args,
    write_product_input,
)
from tests.product.composition_harness import InstalledSources, request_for

_FORBIDDEN_TREE_NAMES = frozenset({"workspace", "trees", "attempts", "HEAD.json"})


def _change_root(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id


def _existing_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    installed_sources: InstalledSources,
    secret: str,
) -> list[str]:
    return [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--secret",
        secret,
        "--json",
    ]


def _assert_no_tree_store(root: Path) -> None:
    if not root.exists():
        return
    names = {path.name for path in root.rglob("*")}
    assert names.isdisjoint(_FORBIDDEN_TREE_NAMES)


def test_two_changes_are_independently_discoverable(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    first_args, project_dir, first_change = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-change-a",
        change_id="CH-A-001",
    )
    second_change = "CH-B-001"
    for change_id in (first_change, second_change):
        case_path = _change_root(project_dir, change_id) / "cases" / "system" / "dept" / "case.yaml"
        case_path.parent.mkdir(parents=True, exist_ok=True)
        case_path.write_text(
            "schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n",
            encoding="utf-8",
        )
    second_input = write_product_input(
        tmp_path / "input-b.json",
        composition,
        change_id=second_change,
        case_delta_paths=(f"qa/changes/{second_change}/cases/system/dept/case.yaml",),
    )
    second_args = [
        "--project-dir",
        str(project_dir),
        "--change",
        second_change,
        "--invocation-id",
        "inv-change-b",
        *source_args(installed_sources),
        "--entrypoint",
        "intake",
        "--input",
        str(second_input),
        "--secret",
        first_args[first_args.index("--secret") + 1],
        "--json",
    ]

    first = cli_runner.invoke(app, ["start", *first_args])
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(app, ["start", *second_args])
    assert second.exit_code == 0, second.output

    first_status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=first_change,
                invocation_id="inv-change-a",
                installed_sources=installed_sources,
                secret=first_args[first_args.index("--secret") + 1],
            ),
        ],
    )
    second_status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=second_change,
                invocation_id="inv-change-b",
                installed_sources=installed_sources,
                secret=first_args[first_args.index("--secret") + 1],
            ),
        ],
    )
    assert first_status.exit_code == 0, first_status.output
    assert second_status.exit_code == 0, second_status.output
    first_doc = parse_json_output(first_status.stdout)
    second_doc = parse_json_output(second_status.stdout)
    assert first_doc["invocation_id"] == "inv-change-a"
    assert second_doc["invocation_id"] == "inv-change-b"
    assert first_doc["change"]["change_id"] == "CH-A-001"
    assert second_doc["change"]["change_id"] == second_change
    first_identity = (
        _change_root(project_dir, first_change)
        / ".runtime"
        / "langgraph"
        / "identities"
        / "inv-change-a.json"
    )
    second_identity = (
        _change_root(project_dir, second_change)
        / ".runtime"
        / "langgraph"
        / "identities"
        / "inv-change-b.json"
    )
    assert first_identity.is_file()
    assert second_identity.is_file()
    assert "runtime" not in json.loads(first_identity.read_bytes())
    assert "runtime" not in json.loads(second_identity.read_bytes())
    assert not (
        _change_root(project_dir, first_change)
        / ".runtime"
        / "langgraph"
        / "identities"
        / "inv-change-b.json"
    ).exists()
    assert not (
        _change_root(project_dir, second_change)
        / ".runtime"
        / "langgraph"
        / "identities"
        / "inv-change-a.json"
    ).exists()
    assert not (_change_root(project_dir, first_change) / ".runtime" / "invocations").exists()
    assert not (_change_root(project_dir, second_change) / ".runtime" / "invocations").exists()


def test_status_and_resume_authenticate_the_change_local_ledger(
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
        invocation_id="inv-auth-001",
        change_id="CH-AUTH-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    secret = args[args.index("--secret") + 1]

    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-auth-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    assert parse_json_output(status.stdout)["invocation_id"] == "inv-auth-001"

    missing_change = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id="CH-MISSING-001",
                invocation_id="inv-auth-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert missing_change.exit_code == 40, missing_change.output

    crossed = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-other-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
            "--action",
            "approve",
            "--reason",
            "accepted",
        ],
    )
    assert crossed.exit_code == 40, crossed.output


def test_failed_run_exposes_status_and_events_but_not_staged_files(
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
        invocation_id="inv-fail-001",
        change_id="CH-FAIL-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    change = _change_root(project_dir, change_id)
    identity = json.loads(
        (change / ".runtime" / "langgraph" / "identities" / "inv-fail-001.json").read_bytes()
    )
    assert "runtime" not in identity
    assert identity["invocation_id"] == "inv-fail-001"
    leftover_invocation = change / ".runtime" / "invocations" / "inv-fail-001"
    assert not leftover_invocation.exists()
    _assert_no_tree_store(project_dir)
    _assert_no_tree_store(change)


def test_loaded_canonical_workflow_binds_agent_execution_contracts(installed_sources) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.product import resolve_assurance_composition
    from graph_engine.composition import CapabilityBindingEntry
    from tests.product.composition_harness import request_for

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    bindings = {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }
    assert set(bindings) == set(AGENT_EXECUTION_CONTRACTS)
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        assert bindings[contract_id].contract_id == contract.contract_id
        assert contract.resources.writes


def test_start_does_not_create_tree_store_directories(
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
        invocation_id="inv-layout-001",
        change_id="CH-LAYOUT-001",
        entrypoint="full",
        families=("api",),
    )
    result = cli_runner.invoke(app, ["start", *args])
    assert result.exit_code == 0, result.output

    change = _change_root(project_dir, change_id)
    runtime = change / ".runtime"
    assert runtime.is_dir()
    langgraph = runtime / "langgraph"
    identity = json.loads((langgraph / "identities" / "inv-layout-001.json").read_bytes())
    binding = json.loads(
        (langgraph / "leases" / "revisions" / "bindings" / "inv-layout-001.json").read_bytes()
    )
    assert "runtime" not in identity
    assert set(binding) == {"invocation_id", "revision_id"}
    assert binding["invocation_id"] == "inv-layout-001"
    assert (langgraph / "checkpoints.sqlite3").is_file()
    assert (change / ".staging").is_dir()
    leftover_invocation = runtime / "invocations" / "inv-layout-001"
    assert not leftover_invocation.exists()
    _assert_no_tree_store(project_dir)
    _assert_no_tree_store(change)
    assert not (tmp_path / "engine-root").exists()


def test_projections_are_not_used_to_advance_execution(
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
        invocation_id="inv-proj-001",
        change_id="CH-PROJ-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    change = _change_root(project_dir, change_id)
    (change / "status.json").write_text('{"status":"forged"}\n', encoding="utf-8")
    (change / "events.jsonl").write_text('{"kind":"forged"}\n', encoding="utf-8")

    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-proj-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    document = parse_json_output(status.stdout)
    assert document["status"] != "forged"
    assert document["invocation_id"] == "inv-proj-001"
    identity = json.loads(
        (change / ".runtime" / "langgraph" / "identities" / "inv-proj-001.json").read_bytes()
    )
    assert "runtime" not in identity
    assert identity["invocation_id"] == "inv-proj-001"
    leftover_invocation = change / ".runtime" / "invocations" / "inv-proj-001"
    assert not leftover_invocation.exists()
