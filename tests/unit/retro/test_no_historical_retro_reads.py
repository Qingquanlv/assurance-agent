"""Production clean-cut guard: no historical Retro readers or legacy enums."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = (
    "qa/retro/_state.json",
    "cross-run-report.json",
    "promotions.json",
    '"workflow_bug"',
    '"issue_export"',
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOT = REPO_ROOT / "assurance_agent"
SKILL_PATH = PRODUCTION_ROOT / "_resources" / "skills" / "aa-retro" / "SKILL.md"
CONTRACTS_PATH = PRODUCTION_ROOT / "_resources" / "schemas" / "execution-contracts.yaml"
WORKFLOW_SCHEMA_PATH = PRODUCTION_ROOT / "_resources" / "schemas" / "workflow-schema.yaml"


def _python_offenders() -> list[str]:
    offenders: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.relative_to(REPO_ROOT)}:{token}" for token in FORBIDDEN if token in text)
    return offenders


def _broad_aa_retro_reads(text: str) -> list[str]:
    """Detect broad skill:aa-retro read grants (not pinned context.json)."""
    hits: list[str] = []
    for needle in (
        "project:qa/retro/**",
        "project:qa/retro/*",
        "qa/retro/*",
        "qa/retro/**",
    ):
        if needle in text:
            hits.append(needle)
    return hits


class _QaRetroWalkVisitor(ast.NodeVisitor):
    """Flag iterdir/glob/rglob rooted at a qa/retro Path expression."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.offenders: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        attr = None
        receiver = None
        if isinstance(func, ast.Attribute) and func.attr in {"iterdir", "glob", "rglob"}:
            attr = func.attr
            receiver = func.value
        if attr is None or receiver is None:
            self.generic_visit(node)
            return
        if self._mentions_qa_retro(receiver):
            self.offenders.append(f"{self.filename}:{node.lineno}:{attr}")
        self.generic_visit(node)

    def _mentions_qa_retro(self, node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if child.value in {"qa", "retro"} or "qa/retro" in child.value:
                    return True
        return False


def _ast_qa_retro_walks() -> list[str]:
    offenders: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _QaRetroWalkVisitor(str(path.relative_to(REPO_ROOT)))
        visitor.visit(tree)
        offenders.extend(visitor.offenders)
    return offenders


def test_production_has_no_historical_retro_reader() -> None:
    offenders = _python_offenders()
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "qa/retro/*" not in skill
    assert "project:qa/retro/**" not in skill
    assert offenders == []


def _contract_block(text: str, key: str) -> str:
    marker = f"  {key}:"
    start = text.index(marker)
    rest = text[start:]
    # Next top-level contract key is indented with exactly two spaces and ends with ':'.
    for idx, line in enumerate(rest.splitlines()[1:], start=1):
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            return "\n".join(rest.splitlines()[:idx])
    return rest


def test_aa_retro_contracts_have_no_broad_retro_reads() -> None:
    contracts = CONTRACTS_PATH.read_text(encoding="utf-8")
    block = _contract_block(contracts, "skill:aa-retro")
    # Only the narrow current-run context pin is allowed for the agent skill.
    assert "project:qa/retro/*/context.json" in block
    for needle in ("project:qa/retro/**", "qa/retro/**"):
        assert needle not in block, f"broad aa-retro read grant: {needle!r}"

    schema = WORKFLOW_SCHEMA_PATH.read_text(encoding="utf-8")
    propose = schema.split("propose-improvements:")[1].split("reconcile-improvements:")[0]
    assert "project:qa/retro/${params.retro_id}/context.json" in propose
    assert "project:qa/retro/**" not in propose


def test_production_has_no_qa_retro_directory_walks() -> None:
    # Reading an explicit current-run path is fine; walking qa/retro is not.
    assert _ast_qa_retro_walks() == []
