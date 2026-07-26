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
    """Flag iterdir/glob/rglob rooted at a qa/retro Path expression.

    Resolves simple in-function Name bindings so patterns like::

        root = project / "qa" / "retro"
        for child in root.iterdir(): ...

    are detected, not only inline ``Path("qa/retro").iterdir()``.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.offenders: list[str] = []
        self._bindings: dict[str, ast.AST] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self._bindings
        self._bindings = dict(previous)
        self.generic_visit(node)
        self._bindings = previous

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        previous = self._bindings
        self._bindings = dict(previous)
        self.generic_visit(node)
        self._bindings = previous

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._bindings[node.targets[0].id] = node.value

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bindings[node.target.id] = node.value

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

    def _mentions_qa_retro(self, node: ast.AST, *, _seen: frozenset[str] | None = None) -> bool:
        """True only when the resolved path expression mentions qa+retro together."""
        seen = set() if _seen is None else set(_seen)
        tokens: set[str] = set()
        stack: list[ast.AST] = [node]
        while stack:
            current = stack.pop()
            for child in ast.walk(current):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    value = child.value
                    if "qa/retro" in value:
                        return True
                    if value in {"qa", "retro"}:
                        tokens.add(value)
                if isinstance(child, ast.Name) and child.id in self._bindings and child.id not in seen:
                    seen.add(child.id)
                    stack.append(self._bindings[child.id])
        return "qa" in tokens and "retro" in tokens


def scan_qa_retro_walks(source: str, *, filename: str = "<snippet>") -> list[str]:
    """Pure AST scanner used by the production guard and unit assertions."""
    tree = ast.parse(source, filename=filename)
    visitor = _QaRetroWalkVisitor(filename)
    visitor.visit(tree)
    return list(visitor.offenders)


def _ast_qa_retro_walks() -> list[str]:
    offenders: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        offenders.extend(
            scan_qa_retro_walks(
                path.read_text(encoding="utf-8"),
                filename=str(path.relative_to(REPO_ROOT)),
            )
        )
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


def test_ast_scan_flags_name_bound_qa_retro_root() -> None:
    snippet = """\
def walk(project):
    root = project / "qa" / "retro"
    for child in root.iterdir():
        pass
"""
    offenders = scan_qa_retro_walks(snippet, filename="synthetic.py")
    assert any(item.endswith(":iterdir") for item in offenders), offenders


def test_ast_scan_flags_chained_name_bound_qa_retro_root() -> None:
    snippet = """\
def walk(project):
    base = project / "qa"
    root = base / "retro"
    for child in root.glob("*"):
        pass
"""
    offenders = scan_qa_retro_walks(snippet, filename="synthetic.py")
    assert any(item.endswith(":glob") for item in offenders), offenders
