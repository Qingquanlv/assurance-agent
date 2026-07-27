"""仓库级收敛验收：`aa workflow status` 只允许出现在精确 allowlist。

扫描面 = 活跃消费面：assurance_agent/（代码 + skills + 插件）、examples/、
README.md、CONTEXT.md、docs/**（排除 docs/superpowers/** 的历史 plan/spec）。
tests/ 不扫描——别名断言合法引用该命令名。
豁免语义（无全局 marker 逃生口）：
- EXEMPT_FILES：整文件豁免（别名实现本体、本 spec）；
- EXEMPT_LINES：按「文件 → 精确行正则」豁免单行；
除此之外的任何命中都失败。文件不按后缀过滤，UTF-8 能解码即扫。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ["assurance_agent", "examples", "README.md", "CONTEXT.md", "docs"]
EXCLUDED_DIRS = {"superpowers"}
EXEMPT_FILES = {
    "assurance_agent/commands/workflow_cmd.py",  # 别名实现本体
    "docs/specs/2026-07-26-status-cli-convergence.md",  # 本 spec
}
EXEMPT_LINES: dict[str, tuple[str, ...]] = {
    # README 命令表中的别名行（Task 4 Step 3 写入的确切行）。
    "README.md": (r"^\| `aa workflow status --change <id>` \| deprecated 别名",),
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


def test_no_unguarded_workflow_status_references() -> None:
    violations = []
    for path, text in iter_text_files():
        rel = str(path.relative_to(REPO))
        if rel in EXEMPT_FILES:
            continue
        patterns = EXEMPT_LINES.get(rel, ())
        for lineno, line in enumerate(text.splitlines(), 1):
            if "aa workflow status" not in line:
                continue
            if any(re.search(pattern, line) for pattern in patterns):
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert not violations, "\n".join(violations)
