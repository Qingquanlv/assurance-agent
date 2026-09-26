from __future__ import annotations

from pathlib import Path

import pytest


def test_preserved_case_revision_survives_later_run(tmp_path: Path) -> None:
    from assurance_product.run_history import preserve_run_output, read_preserved_output

    current = tmp_path / "case.yaml"
    current.write_bytes(b"case: A\n")
    run_dir = tmp_path / "run-a"
    ref = preserve_run_output(
        run_dir=run_dir,
        change_id="change-a",
        node_id="case-design",
        activation_id="activation-a",
        attempt_key="attempt-a",
        kind="cases",
        logical_path="qa/cases/user/case.yaml",
        content=current.read_bytes(),
    )
    current.write_bytes(b"case: B\n")
    assert read_preserved_output(run_dir, ref) == b"case: A\n"


def test_two_attempts_keep_distinct_bytes(tmp_path: Path) -> None:
    from assurance_product.run_history import preserve_run_output, read_preserved_output

    run_dir = tmp_path / "run-a"
    first = preserve_run_output(
        run_dir=run_dir,
        change_id="change-a",
        node_id="case-design",
        activation_id="activation-1",
        attempt_key="attempt-1",
        kind="cases",
        logical_path="qa/cases/user/case.yaml",
        content=b"case: A\n",
    )
    second = preserve_run_output(
        run_dir=run_dir,
        change_id="change-a",
        node_id="case-design",
        activation_id="activation-2",
        attempt_key="attempt-2",
        kind="cases",
        logical_path="qa/cases/user/case.yaml",
        content=b"case: B\n",
    )
    assert first.output_id != second.output_id
    assert read_preserved_output(run_dir, first) == b"case: A\n"
    assert read_preserved_output(run_dir, second) == b"case: B\n"


def test_corrupted_preserved_bytes_are_unavailable(tmp_path: Path) -> None:
    from assurance_product.run_history import RunHistoryError, preserve_run_output, read_preserved_output

    run_dir = tmp_path / "run-a"
    ref = preserve_run_output(
        run_dir=run_dir,
        change_id="change-a",
        node_id="case-design",
        activation_id="activation-a",
        attempt_key="attempt-a",
        kind="cases",
        logical_path="qa/cases/user/case.yaml",
        content=b"case: A\n",
    )
    Path(ref.stored_path).write_bytes(b"case: tampered\n")
    with pytest.raises(RunHistoryError, match="digest does not match"):
        read_preserved_output(run_dir, ref)


def test_promoted_attempt_bytes_bind_to_their_own_node_after_later_write(tmp_path: Path) -> None:
    from assurance_product.run_history import (
        capture_attempt_files,
        publish_attempt_outputs,
        read_preserved_output,
    )

    run_dir = tmp_path / "run-a"
    capture_attempt_files(run_dir, "1" * 64, (("qa/cases/user/case.yaml", b"case: A\n"),))
    capture_attempt_files(run_dir, "2" * 64, (("qa/cases/user/case.yaml", b"case: B\n"),))

    first = publish_attempt_outputs(
        run_dir, "1" * 64, "change-a", "intake.case-design/finalize", "activation-1", None
    )
    second = publish_attempt_outputs(
        run_dir, "2" * 64, "change-a", "intake.case-review/finalize", "activation-2", None
    )

    assert len(first) == len(second) == 1
    assert first[0].kind == second[0].kind == "artifact"
    assert first[0].node_id == "intake.case-design/finalize"
    assert second[0].node_id == "intake.case-review/finalize"
    assert read_preserved_output(run_dir, first[0]) == b"case: A\n"
    assert read_preserved_output(run_dir, second[0]) == b"case: B\n"


def test_repeated_history_reads_do_not_republish_unchanged_outputs(tmp_path: Path, monkeypatch) -> None:
    import assurance_product.run_history as history

    run_dir = tmp_path / "run"
    history.capture_attempt_files(run_dir, "1" * 64, (("qa/result.json", b"{}"),))
    first = history.publish_attempt_outputs(run_dir, "1" * 64, "change", "node", "activation", None)

    def forbidden(*_args, **_kwargs) -> None:
        raise AssertionError("unchanged output was republished")

    monkeypatch.setattr(history, "_publish_ref", forbidden)
    assert history.publish_attempt_outputs(run_dir, "1" * 64, "change", "node", "activation", None) == first


def test_concurrent_capture_of_identical_bytes_keeps_both_attempts(tmp_path: Path, monkeypatch) -> None:
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from assurance_product.run_history import capture_attempt_files, publish_attempt_outputs

    barrier = Barrier(2)
    replace = os.replace

    def simultaneous_replace(source, destination, *args, **kwargs):
        if Path(destination).parent.name == "objects":
            barrier.wait(timeout=5)
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", simultaneous_replace)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(capture_attempt_files, tmp_path, digit * 64, (("qa/result.json", b"{}"),))
            for digit in ("1", "2")
        ]
        for future in futures:
            future.result()
    for digit in ("1", "2"):
        refs = publish_attempt_outputs(tmp_path, digit * 64, "change", "node", digit, None)
        assert len(refs) == 1
        assert refs[0].logical_path == "qa/result.json"


def test_concurrent_output_publication_keeps_all_references(tmp_path: Path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError

    from assurance_product.run_history import preserve_run_output, preserved_refs

    manifest = tmp_path / "history/outputs.json"
    manifest.parent.mkdir()
    manifest.write_text("[]")
    barrier = Barrier(2)
    read_text = Path.read_text

    def overlapping_read(path, *args, **kwargs):
        content = read_text(path, *args, **kwargs)
        if path == manifest:
            try:
                barrier.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
        return content

    monkeypatch.setattr(Path, "read_text", overlapping_read)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(
                preserve_run_output,
                run_dir=tmp_path,
                change_id="change",
                node_id="node",
                activation_id=digit,
                attempt_key=digit,
                kind="artifact",
                logical_path="qa/result.json",
                content=digit.encode(),
            )
            for digit in ("1", "2")
        ]
        for future in futures:
            future.result()
    assert {ref.attempt_key for ref in preserved_refs(tmp_path)} == {"1", "2"}


def test_history_capture_failure_does_not_block_promotion(tmp_path: Path, caplog) -> None:
    import asyncio
    import hashlib

    from assurance_product.run_history import RunOutputWorkspaceProvider
    from graph_engine.attempts.workspace import TaskWorkspaceStore

    project = tmp_path / "project"
    project.mkdir()
    run_dir = project / ".aa/runs/change-a"
    objects = run_dir / "history/objects"
    objects.mkdir(parents=True)
    content = b'{"result":"committed"}'
    (objects / hashlib.sha256(content).hexdigest()).write_bytes(b"corrupted display copy")
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    provider = RunOutputWorkspaceProvider(store, run_dir)
    try:
        binding = store.begin(task_id="1" * 64, attempt=1, output_paths=("qa/result.json",))
        staged = binding.write_root / "qa/result.json"
        staged.parent.mkdir()
        staged.write_bytes(content)
        prepared = store.prepare_sealed(binding, store.seal_complete(binding))

        asyncio.run(provider.promote(prepared))

        assert (project / "qa/result.json").read_bytes() == content
        assert "promoted output digest does not match" in caplog.text
    finally:
        store.close()


def test_workspace_promotion_captures_committed_bytes(tmp_path: Path) -> None:
    import asyncio

    from assurance_product.run_history import RunOutputWorkspaceProvider, publish_attempt_outputs
    from graph_engine.attempts.workspace import TaskWorkspaceStore

    project = tmp_path / "project"
    project.mkdir()
    run_dir = project / ".aa" / "runs" / "change-a"
    run_dir.mkdir(parents=True)
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    provider = RunOutputWorkspaceProvider(store, run_dir)
    binding = store.begin(task_id="1" * 64, attempt=1, output_paths=("qa/result.json",))
    staged = binding.write_root / "qa" / "result.json"
    staged.parent.mkdir()
    staged.write_bytes(b'{"result":"first"}')
    sealed = store.seal_complete(binding)
    prepared = store.prepare_sealed(binding, sealed)

    asyncio.run(provider.promote(prepared))

    refs = publish_attempt_outputs(run_dir, "1" * 64, "change-a", "node", "activation", None)
    assert len(refs) == 1
    assert refs[0].logical_path == "qa/result.json"
    assert (project / "qa" / "result.json").read_bytes() == b'{"result":"first"}'
