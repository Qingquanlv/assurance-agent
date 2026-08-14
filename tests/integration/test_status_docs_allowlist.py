"""仓库级收敛验收：活跃消费面不得再引用已删除的 `aa workflow status`。

扫描面 = 活跃消费面：assurance_agent/（代码 + skills + 插件）、examples/、
README.md、CONTEXT.md、docs/**（排除 docs/superpowers/** 的历史 plan/spec）。
tests/ 不扫描——测试可引用该命令名做负面断言。
豁免：仅保留历史 spec 文档（记录收敛过程本身）。
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ["assurance_agent", "examples", "README.md", "CONTEXT.md", "docs"]
EXCLUDED_DIRS = {"superpowers"}
EXEMPT_FILES = {
    "docs/specs/2026-07-26-status-cli-convergence.md",  # historical convergence spec
}


def iter_text_files():
    for root in SCAN_ROOTS:
        base = REPO / root
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in files:
            if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 二进制（__pycache__/.DS_Store 等）不扫描
            yield path, text


def test_no_workflow_status_references() -> None:
    violations = []
    for path, text in iter_text_files():
        rel = str(path.relative_to(REPO))
        if rel in EXEMPT_FILES:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "aa workflow status" not in line:
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert not violations, "\n".join(violations)
