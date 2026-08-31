from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
FEATURES = (
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-quality",
    "assurance-healing",
    "assurance-improvement",
)


def dependencies(path: Path) -> set[str]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return set(document["project"]["dependencies"])


def test_langgraph_dependencies_are_direct_and_exact() -> None:
    engine = dependencies(ROOT / "packages/framework/graph-engine/pyproject.toml")
    assert "langgraph==1.2.11" in engine
    assert "langgraph-checkpoint==4.2.0" in engine
    for feature in FEATURES:
        feature_deps = dependencies(ROOT / f"packages/capabilities/{feature}/pyproject.toml")
        assert "langgraph==1.2.11" in feature_deps
    product = dependencies(ROOT / "packages/products/assurance-product/pyproject.toml")
    assert "langgraph==1.2.11" in product
    assert "langgraph-checkpoint-sqlite==3.1.1" in product
