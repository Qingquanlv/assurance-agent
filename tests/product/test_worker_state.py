from __future__ import annotations

import json
from pathlib import Path

import pytest


def _saved_owner(tmp_path: Path):
    from assurance_product.worker_lifecycle import acquire_execution
    from assurance_product.worker_state import control_root

    with acquire_execution(tmp_path, "run"):
        pass
    path = control_root(tmp_path) / "owner.json"
    return path, json.loads(path.read_text())


@pytest.mark.parametrize(
    "mutation",
    [
        {"process": {"pid": 123}},
        {"directory": [1]},
        {"calls": "not-a-list"},
        {"attempts": [1]},
        {"state": "unknown"},
        {"schema": True},
        {"unrecognized_evidence": ["pending"]},
    ],
)
def test_invalid_owner_record_blocks_admission_and_signaling(tmp_path, monkeypatch, mutation):
    from assurance_product import worker_lifecycle as lifecycle
    from assurance_product.worker_state import ExecutionConflict

    path, record = _saved_owner(tmp_path)
    record.update(mutation)
    path.write_text(json.dumps(record))
    monkeypatch.setattr(lifecycle, "terminate", lambda *args: pytest.fail("signaled invalid owner"))
    with pytest.raises(ExecutionConflict):
        lifecycle.request_stop(tmp_path, force=True)
    with pytest.raises(ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "replacement"):
            pytest.fail("admitted invalid owner")
    assert json.loads(path.read_text()) == record


def test_owner_record_preserves_schema_one_and_optional_evidence(tmp_path):
    from assurance_product.worker_state import read_owner, write_owner

    path, record = _saved_owner(tmp_path)
    for key in ("attempts", "servers", "launching_service"):
        record.pop(key)
    record["stop_authority"] = {"authorization": {"secret_sources": []}}
    path.write_text(json.dumps(record))
    saved = read_owner(path.parent)
    assert saved == record
    assert saved is not None
    write_owner(path.parent, saved)
    assert json.loads(path.read_text()) == record


def test_corrupt_owner_json_fails_closed(tmp_path):
    from assurance_product.worker_state import ExecutionConflict, read_owner

    path, _ = _saved_owner(tmp_path)
    path.write_text("{broken")
    with pytest.raises(ExecutionConflict, match="invalid worker ownership record"):
        read_owner(path.parent)
