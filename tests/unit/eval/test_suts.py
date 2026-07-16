from __future__ import annotations

from pathlib import Path

import yaml

from assurance_agent.eval.suts import load_sut_registry, resolve_sut_dir


def test_resolve_sut_dir_from_registry(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    (engine / "eval").mkdir(parents=True)
    (engine / "eval" / "suts.yaml").write_text(
        yaml.safe_dump(
            {
                "suts": {
                    "vue-fastapi-admin": {
                        "local_dir": "benchmark/vue-fastapi-admin",
                        "pinned_rev": "rev-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (engine / "benchmark" / "vue-fastapi-admin").mkdir(parents=True)
    registry = load_sut_registry(engine)
    assert "vue-fastapi-admin" in registry.suts
    assert resolve_sut_dir(engine) == (engine / "benchmark" / "vue-fastapi-admin").resolve()
