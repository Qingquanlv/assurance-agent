from __future__ import annotations

import importlib.util
import hashlib
import sys
from pathlib import Path

import pytest
import yaml

from assurance_generation.operations.init_runtime import (
    DATA_KNOWLEDGE_PATH,
    DATA_KNOWLEDGE_RESOURCE_ID,
    HARNESS_PATHS,
    InitTestRuntimeHandler,
    collect_l1_symbols,
    symbol_to_repo_path,
)
from tests.product.test_change_local_output_routing import dual_roots, execute_task

_SHA = "a" * 64


def test_generic_runtime_settings_do_not_define_admin_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QA_ADMIN_USERNAME", "ignored-user")
    monkeypatch.setenv("QA_ADMIN_PASSWORD", "ignored-password")
    config_path = (
        Path(__file__).resolve().parent.parent / "assurance_generation/resources/test-runtime/tests/config.py"
    )
    spec = importlib.util.spec_from_file_location("assurance_test_runtime_config", config_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    settings = module.load_settings()
    assert settings.base_url
    assert settings.frontend_url
    assert not hasattr(settings, "admin_username")
    assert not hasattr(settings, "admin_password")


def _knowledge() -> dict[str, object]:
    return {
        "version": 1,
        "capabilities": {
            "domain_factories": {
                "dept": {
                    "make_dept": {
                        "kind": "async_factory",
                        "symbol": "tests.testdata.domain.dept.make_dept",
                        "notes": "Create one department",
                    },
                    "cleanup_dept": {
                        "kind": "async_factory",
                        "symbol": "tests.testdata.domain.dept.cleanup_dept",
                    },
                }
            },
            "adapters": {
                "api": {
                    "dept": {
                        "make_dept": {
                            "kind": "isolated_worker",
                            "symbol": "tests.api.adapters.dept.factory_make_dept",
                        }
                    }
                }
            },
        },
        "auth": {
            "api_admin_token": {"symbol": "tests.api.conftest.admin_token"},
        },
    }


def test_collects_only_testdata_and_family_adapter_symbols() -> None:
    symbols = collect_l1_symbols(_knowledge())
    assert [item.symbol for item in symbols] == [
        "tests.api.adapters.dept.factory_make_dept",
        "tests.testdata.domain.dept.cleanup_dept",
        "tests.testdata.domain.dept.make_dept",
    ]


def test_symbol_maps_to_qa_tests_module() -> None:
    path, name = symbol_to_repo_path("tests.testdata.domain.dept.make_dept")
    assert path == "qa/tests/testdata/domain/dept.py"
    assert name == "make_dept"


@pytest.mark.parametrize(
    "symbol",
    (
        "tests.testdata.evil../x.foo",
        "tests.testdata.test_hidden.foo",
        "tests.api.adapters.dept.locustfile_helper",
    ),
)
def test_unsafe_symbol_is_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="unsafe|test_|locustfile"):
        symbol_to_repo_path(symbol)


@pytest.mark.asyncio
async def test_handler_creates_harness_and_skeletons(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    knowledge = yaml.safe_dump(_knowledge()).encode("utf-8")
    knowledge_path = project / DATA_KNOWLEDGE_PATH
    knowledge_path.parent.mkdir(parents=True)
    knowledge_path.write_bytes(knowledge)
    digest = hashlib.sha256(knowledge).hexdigest()
    outcome = await execute_task(
        InitTestRuntimeHandler(),
        {
            "change_id": "CH-DEMO-001",
            "data_knowledge": {"resource_id": DATA_KNOWLEDGE_RESOURCE_ID, "sha256": digest},
            "capability_leafs": ["dept.crud.create"],
            "allowed_artifact_paths": [
                "qa/.qa.yaml",
                "qa/cases",
                "qa/fixtures",
                "qa/proposal.md",
                "qa/requirement.md",
                "qa/results",
                "qa/tests",
            ],
        },
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded"
    result = outcome.output
    assert isinstance(result, dict)
    harness_created = result["harness_created"]
    symbol_files_created = result["symbol_files_created"]
    assert isinstance(harness_created, list)
    assert isinstance(symbol_files_created, list)
    assert set(HARNESS_PATHS) <= set(harness_created)
    assert "qa/tests/testdata/__init__.py" in symbol_files_created
    assert "qa/tests/testdata/domain/__init__.py" in symbol_files_created
    assert "qa/tests/testdata/domain/dept.py" not in symbol_files_created
    assert not (write_root / "qa/tests/testdata/domain/dept.py").exists()
    assert (write_root / "qa/tests/testdata/domain").is_dir()
    assert "qa/tests/api/adapters/dept.py" in symbol_files_created
    module = (write_root / "qa/tests/api/adapters/dept.py").read_text(encoding="utf-8")
    assert "def factory_make_dept(*args, **kwargs)" in module
    assert 'raise NotImplementedError("assurance.init: tests.api.adapters.dept.factory_make_dept")' in module
    assert not (write_root / "qa/tests/api/conftest.py").read_text(encoding="utf-8").count("dept")
    receipt = write_root / "qa/results/init/test-runtime.json"
    assert receipt.is_file()


@pytest.mark.asyncio
async def test_second_run_skips_existing_regular_files(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    knowledge = yaml.safe_dump(_knowledge()).encode("utf-8")
    path = project / DATA_KNOWLEDGE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(knowledge)
    digest = hashlib.sha256(knowledge).hexdigest()
    payload = {
        "change_id": "CH-DEMO-001",
        "data_knowledge": {"resource_id": DATA_KNOWLEDGE_RESOURCE_ID, "sha256": digest},
        "capability_leafs": [],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
    }
    first = await execute_task(InitTestRuntimeHandler(), payload, project, write_root=write_root)
    assert first.status == "succeeded"
    # Simulate Kernel seal: copy staged files onto the durable tree.
    for staged in write_root.rglob("*"):
        if staged.is_file():
            durable = project / staged.relative_to(write_root)
            durable.parent.mkdir(parents=True, exist_ok=True)
            durable.write_bytes(staged.read_bytes())
    original = (project / "qa/tests/api/adapters/dept.py").read_bytes()
    second_root = project / "qa" / ".staging" / "attempt-2"
    second_root.mkdir(parents=True)
    second = await execute_task(InitTestRuntimeHandler(), payload, project, write_root=second_root)
    assert second.status == "succeeded"
    second_output = second.output
    assert isinstance(second_output, dict)
    symbol_files_skipped = second_output["symbol_files_skipped"]
    assert isinstance(symbol_files_skipped, list)
    assert "qa/tests/api/adapters/dept.py" in symbol_files_skipped
    assert (project / "qa/tests/api/adapters/dept.py").read_bytes() == original
    assert not (second_root / "qa/tests/api/adapters/dept.py").exists()
    assert not (project / "qa/tests/testdata/domain/dept.py").exists()
    assert not (second_root / "qa/tests/testdata/domain/dept.py").exists()


@pytest.mark.asyncio
async def test_symlink_target_fails(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    knowledge = yaml.safe_dump(_knowledge()).encode("utf-8")
    (project / DATA_KNOWLEDGE_PATH).parent.mkdir(parents=True)
    (project / DATA_KNOWLEDGE_PATH).write_bytes(knowledge)
    target = project / "qa/tests/conftest.py"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "outside.py")
    outcome = await execute_task(
        InitTestRuntimeHandler(),
        {
            "change_id": "CH-DEMO-001",
            "data_knowledge": {
                "resource_id": DATA_KNOWLEDGE_RESOURCE_ID,
                "sha256": hashlib.sha256(knowledge).hexdigest(),
            },
            "capability_leafs": [],
            "allowed_artifact_paths": [
                "qa/.qa.yaml",
                "qa/cases",
                "qa/fixtures",
                "qa/proposal.md",
                "qa/requirement.md",
                "qa/results",
                "qa/tests",
            ],
        },
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_digest_mismatch_fails(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    (project / DATA_KNOWLEDGE_PATH).parent.mkdir(parents=True)
    (project / DATA_KNOWLEDGE_PATH).write_bytes(b"version: 1\n")
    outcome = await execute_task(
        InitTestRuntimeHandler(),
        {
            "change_id": "CH-DEMO-001",
            "data_knowledge": {"resource_id": DATA_KNOWLEDGE_RESOURCE_ID, "sha256": _SHA},
            "capability_leafs": [],
            "allowed_artifact_paths": [
                "qa/.qa.yaml",
                "qa/cases",
                "qa/fixtures",
                "qa/proposal.md",
                "qa/requirement.md",
                "qa/results",
                "qa/tests",
            ],
        },
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
