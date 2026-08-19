from pathlib import Path

from assurance_agent.workflow.core.tree_hash import sha256_file


def test_sha256_file_hashes_bytes(tmp_path: Path) -> None:
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    digest = sha256_file(path)
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
