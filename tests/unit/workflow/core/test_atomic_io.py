from pathlib import Path

from assurance_agent.workflow.core.atomic_io import atomic_write_bytes


def test_atomic_write_bytes_replaces_file(tmp_path: Path) -> None:
    path = tmp_path / "out.bin"
    atomic_write_bytes(path, b"one")
    atomic_write_bytes(path, b"two")
    assert path.read_bytes() == b"two"
