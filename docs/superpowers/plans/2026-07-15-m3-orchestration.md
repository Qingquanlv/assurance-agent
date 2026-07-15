# M3 — 编排核心（Schema / DSL / DAG / Gate / State / Events）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付纯库层的编排核心——workflow-schema 加载与静态校验、三值谓词 DSL、DAG 状态引擎、闭集 Gate 裁决、typed event/state 原语，以及完整的纯 healing episode projection（entry→allocation→apply→rerun→reinspect→continue/STOP）——为 M4/M6 提供全部确定性读侧逻辑。本里程碑不含 CLI 命令；M6 只消费这里冻结的 snapshot/event 合同实现写边界。

**Architecture:** DSL 用 `ast.parse` + 白名单自解释，绝不 `eval`。`WorkflowSchema` 加载期拒绝未知 verdict、重复 phase、phase/gate 环和坏引用；schema 与 Gate 输出共用 `Verdict(StrEnum)`。`compute_status` 保持 produces-existence 的纯 DAG 投影；共享旧产物无法表达的 healing 重入由独立 `project_healing_episode` 根据 typed allocation/outcome ledger 投影，二者汇聚为 `WorkflowStatus`。`attempts_used` 的唯一事实源是当前 episode 的 `healing_attempt_allocated`（按 `operation_id` 去重）；state 中的同名字段只作序列化展示并在求值前被事件投影覆盖。M6 使用 M3 的 `capture_files/restore_files` 完成 strict-event 写边界，不另立 WAL。

**Tech Stack:** Python 3.11+, uv, pydantic v2, PyYAML, `ast`（标准库）, `hashlib`, pytest, ruff, pyright, import-linter。

## Global Constraints

- CLI 命令名 `aa`；Python 包名 `assurance_agent`；项目配置目录 `.aa/`；资源文件不得残留 `aws` 字样。
- 工具链固定：uv + pyproject.toml + pydantic v2 + PyYAML + ruff + pyright + pytest + import-linter。
- 包内任何模块禁止用源码仓库相对路径（`Path(__file__)/..`）定位 `_resources/`；只能经 `assurance_agent/resources.py` 访问（M1 已落地：`read_text` / `exists` / `iter_children`）。
- 分层约束（import-linter，与系列「Import 契约（冻结）」一致）：层次自上而下为 `commands → workflow.orchestration / workflow.execution / workflow.report → workflow.core → artifacts → config / resources`。本里程碑：`orchestration` 可 import `workflow.core`、`artifacts`、`resources`、`config`；`workflow.core`（`state.py`/`events.py`/`exit_codes.py`）**可** import `artifacts`（读写 canonical `WorkflowState`）与 `resources`/`config`，但**不得** import `orchestration`（保持 core 在 orchestration 之下）。`artifacts` 位于 `workflow.core` **之下**，不得 import 任何 `workflow.*`。
- 禁止 `eval` / `exec` / `compile(mode='eval')` 执行 DSL 表达式；DSL 只允许 `ast.parse` 解析 + 自解释。
- 每个 Task 结束必须 `git commit`（conventional commits：feat/test/chore/refactor）；每个 Task 收尾 `uv run ruff check .`、`uv run pyright`、`uv run lint-imports` 全绿。
- 所有 pytest 命令在仓库根目录运行：`uv run pytest <path> -v`。

## 消费与产出接口（与计划系列「接口契约」对齐）

- 消费（M1 已落地）：`assurance_agent.resources.read_text/exists/iter_children`；`assurance_agent.config.load_config` / `AaConfig`；`assurance_agent.exceptions.AaError`。
- 消费（**M2 是 M3 的前置里程碑，实现 M3 前 M2 必须已落地**）：`WorkflowState` 及其显式子模型 `WorkflowPhases` / `HealingPhaseState` / `WorkflowGates` / `RunContext`。已知字段使用属性访问；动态 phase 仅经兼容扩展层读取。**不提供 dict 占位，也不允许顶层 `state.healing`**。
- 产出（M4/M5/M6/M8 消费）：见下方各 Task 的 **Interfaces → Produces**，签名以此为准，与计划系列「接口契约」M3 段一致。

---

### Task 1: 退出码常量（`workflow/core/exit_codes.py`）

**Files:**
- Create: `assurance_agent/workflow/__init__.py`（若不存在，空文件）
- Create: `assurance_agent/workflow/core/__init__.py`（若不存在，空文件；M1 已建 `workflow/core/`，确认存在则跳过创建）
- Create: `assurance_agent/workflow/core/exit_codes.py`
- Test: `tests/unit/__init__.py`（空文件）、`tests/unit/test_exit_codes.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - 常量 `EXIT_COMPLETED = 0`、`EXIT_STOPPED = 20`、`EXIT_HUMAN_REVIEW = 30`、`EXIT_ERROR = 40`、`EXIT_USAGE = 2`。
  - `exit_code_for_gate_verdict(verdict: str) -> int`。
  - `exit_code_for_terminal(terminal: TerminalLike | None) -> int`——`TerminalLike` 为结构化协议（任意带 `.kind: str` 的对象），避免 `core` 反向 import `orchestration.engine.Terminal`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_exit_codes.py
from dataclasses import dataclass

from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    exit_code_for_gate_verdict,
    exit_code_for_terminal,
)


@dataclass
class _T:
    kind: str


def test_terminal_mapping():
    assert exit_code_for_terminal(None) == EXIT_COMPLETED
    assert exit_code_for_terminal(_T("completed")) == EXIT_COMPLETED
    assert exit_code_for_terminal(_T("stopped")) == EXIT_STOPPED
    assert exit_code_for_terminal(_T("needs_human_review")) == EXIT_HUMAN_REVIEW
    assert exit_code_for_terminal(_T("future-terminal-kind")) == EXIT_ERROR  # fail-closed


def test_gate_verdict_mapping():
    for v in ("pass", "enter", "exit", "skip"):
        assert exit_code_for_gate_verdict(v) == EXIT_COMPLETED
    for v in ("needs_fix", "needs_human_review", "continue"):
        assert exit_code_for_gate_verdict(v) == EXIT_HUMAN_REVIEW
    for v in ("reject", "stop"):
        assert exit_code_for_gate_verdict(v) == EXIT_ERROR
    assert exit_code_for_gate_verdict("banana") == EXIT_ERROR  # fail-closed
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_exit_codes.py -v`
Expected: FAIL（`ModuleNotFoundError: assurance_agent.workflow.core.exit_codes`）

- [ ] **Step 3: 写实现**

```python
# assurance_agent/workflow/core/exit_codes.py
"""CLI/driver 退出码常量与映射（数值对齐源版 CliExitCodes）。

completed/running=0 / stopped=20 / humanReview=30 / command-or-data-error=40。
"""
from __future__ import annotations

from typing import Protocol

EXIT_COMPLETED = 0
EXIT_STOPPED = 20
EXIT_HUMAN_REVIEW = 30
EXIT_ERROR = 40
EXIT_USAGE = 2


class TerminalLike(Protocol):
    kind: str


def exit_code_for_gate_verdict(verdict: str) -> int:
    if verdict in ("pass", "enter", "exit", "skip"):
        return EXIT_COMPLETED
    if verdict in ("needs_fix", "needs_human_review", "continue"):
        return EXIT_HUMAN_REVIEW
    # reject / stop / 未知 → fail-closed
    return EXIT_ERROR


def exit_code_for_terminal(terminal: TerminalLike | None) -> int:
    if terminal is None:
        return EXIT_COMPLETED
    if terminal.kind == "completed":
        return EXIT_COMPLETED
    if terminal.kind == "stopped":
        return EXIT_STOPPED
    if terminal.kind == "needs_human_review":
        return EXIT_HUMAN_REVIEW
    # 未知 terminal 不能被误报为成功；新增种类必须先显式冻结退出码语义。
    return EXIT_ERROR
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_exit_codes.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/__init__.py assurance_agent/workflow/core/__init__.py \
        assurance_agent/workflow/core/exit_codes.py tests/unit/__init__.py tests/unit/test_exit_codes.py
git commit -m "feat: add workflow exit code constants and verdict/terminal mappings"
```

---

### Task 2: DSL 语法解析 + 白名单（`workflow/orchestration/dsl.py` 上半）

DSL 是一门**独立小语言**，其语法恰好是 Python 表达式的子集。用 `ast.parse(text, mode="eval")` 只做语法解析，随后用白名单把 Python AST 转成本地 `Expr` 节点；白名单之外的节点在解析期即抛 `DslError`。禁止 `eval`/`exec`。

支持的语法（对齐源版 tokenizer/parser 语法契约）：
- 字面量：单/双引号字符串、整数/小数（可带一元负号）、关键字 `true` / `false` / `null`（映射为 `True` / `False` / `None`，**不是**标识符）。
- 列表字面量 `['a', 'b']`（元素只能是字面量）。
- 标识符与成员访问 `params.test_types`、`state.phases.execution.status`；常量下标访问 `state.phases['skill-registry-check'].status`、`x[0]`（下标只能是字符串/整数常量；`x.k` 与 `x['k']` 等价）。
- 比较 `== != < <= > >=`、成员 `in` / `not in`。
- 布尔 `and` / `or` / `not`、括号分组。
- 函数调用（白名单）：`len(x)`、`defined(x)`、`file_exists(p)`、`gate(id)`、`any(coll, pred)`、`all(coll, pred)`、`count(coll, pred)`。二参数 `any/all/count` 的第二参数是隐式 lambda 表达式，解析后**保留其 AST 节点**，求值期对集合每个元素在子作用域重求值。

**Files:**
- Create: `assurance_agent/workflow/orchestration/__init__.py`（空文件）
- Create: `assurance_agent/workflow/orchestration/dsl.py`
- Test: `tests/unit/test_dsl_parse.py`

**Interfaces:**
- Consumes: 无。
- Produces（本 Task 部分）：
  - 异常 `class DslError(AaError)`。
  - AST 节点（frozen dataclass）：`Literal(value)`、`ListLit(elements: tuple[object,...])`、`Ident(name: str)`、`Member(obj: Expr, prop: str)`、`Subscript(obj: Expr, index: object)`（常量 str/int 下标，Spec:77）、`Compare(op: str, left: Expr, right: Expr)`、`BoolOp(op: Literal["and","or"], left: Expr, right: Expr)`、`Not(operand: Expr)`、`Call(callee: str, args: tuple[Expr,...])`；类型别名 `Expr = Literal | ListLit | Ident | Member | Subscript | Compare | BoolOp | Not | Call`。
  - `BUILTIN_ARITY: dict[str, int]`（`len/file_exists/defined` →1，`any/all/count`→2，`gate`→1）。
  - `parse_expression(text: str) -> Expr`（含长度/深度上限；语法或白名单错误抛 `DslError`）。
  - `collect_calls(expr: Expr) -> list[Call]`、`collect_gate_refs(expr: Expr) -> list[str]`（供 schema 静态校验）。
  - 常量 `MAX_EXPR_LEN = 2000`、`MAX_DEPTH = 40`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_dsl_parse.py
import pytest

from assurance_agent.workflow.orchestration.dsl import (
    BoolOp,
    Call,
    Compare,
    DslError,
    Ident,
    Literal,
    Member,
    Subscript,
    collect_gate_refs,
    parse_expression,
)


def test_keyword_literals_map():
    assert parse_expression("true") == Literal(True)
    assert parse_expression("false") == Literal(False)
    assert parse_expression("null") == Literal(None)


def test_member_and_compare():
    node = parse_expression("params.run_mode == 'full'")
    assert isinstance(node, Compare)
    assert node.op == "=="
    assert isinstance(node.left, Member)
    assert node.left.prop == "run_mode"
    assert isinstance(node.left.obj, Ident)
    assert node.right == Literal("full")


def test_in_and_boolop():
    node = parse_expression("'api' in params.test_types and params.run_tests == true")
    assert isinstance(node, BoolOp)
    assert node.op == "and"


def test_two_arg_any_keeps_predicate_ast():
    node = parse_expression("any(fix_proposal.proposals, target == 'api' and eligible == true)")
    assert isinstance(node, Call)
    assert node.callee == "any"
    assert len(node.args) == 2
    assert isinstance(node.args[1], BoolOp)  # 谓词保留为 AST


def test_gate_ref_collection():
    node = parse_expression("gate('healing-entry-gate').verdict == 'enter'")
    assert collect_gate_refs(node) == ["healing-entry-gate"]


def test_subscript_constant_index():
    node = parse_expression("state.phases['skill-registry-check'].status == 'pass'")
    assert isinstance(node, Compare)
    assert isinstance(node.left, Member)          # .status
    assert isinstance(node.left.obj, Subscript)   # ['skill-registry-check']
    assert node.left.obj.index == "skill-registry-check"


def test_rejects_non_constant_subscript():
    with pytest.raises(DslError):
        parse_expression("state.phases[params.x]")  # 非常量下标不允许


def test_rejects_disallowed_nodes():
    with pytest.raises(DslError):
        parse_expression("__import__('os')")     # Call 到非白名单函数
    with pytest.raises(DslError):
        parse_expression("a + b")                # 二元算术不在白名单
    with pytest.raises(DslError):
        parse_expression("lambda x: x")          # lambda 不允许


def test_rejects_bare_equals():
    with pytest.raises(DslError):
        parse_expression("a = b")                # 语法错误（Python 赋值非表达式）
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_dsl_parse.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3a: 定义 frozen AST 节点、`Expr` union 与异常；运行 `pyright`**

- [ ] **Step 3b: 实现 literal/member/subscript/compare/bool/not 转换；运行 `uv run pytest tests/unit/test_dsl_parse.py -v -k 'literal or member or subscript or rejects_bare_equals'`**

- [ ] **Step 3c: 实现 call 白名单、arity 校验、`collect_calls` / `collect_gate_refs`；运行完整 parser 测试**

以下代码块是 Steps 3a–3c 完成后的 `dsl.py` 上半部参考结果；按上述切片逐段落地，不一次粘贴后才测试：

```python
# assurance_agent/workflow/orchestration/dsl.py
"""AA 谓词小语言（DSL）：ast.parse 语法解析 + 白名单自解释 + 三值求值。

- 值模型：缺失路径 → MISSING（不是 None；None 是显式 null）。求值对缺失数据是全函数，
  不抛异常；仅结构性错误（未知函数、arity 错误、缺失 gate/file 解析器）抛 DslError。
- 语义严格对齐源版 dsl/evaluator.ts（三值真值表、typed 相等、any/all/count 隐式 lambda）。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from assurance_agent.exceptions import AaError


class DslError(AaError):
    """DSL 表达式非法或求值期结构性错误。"""


MAX_EXPR_LEN = 2000
MAX_DEPTH = 40

BUILTIN_ARITY: dict[str, int] = {
    "len": 1,
    "file_exists": 1,
    "defined": 1,
    "any": 2,
    "all": 2,
    "count": 2,
    "gate": 1,
}

_KEYWORD_LITERALS: dict[str, object] = {"true": True, "false": False, "null": None}
_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
}


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class ListLit:
    elements: tuple[object, ...]


@dataclass(frozen=True)
class Ident:
    name: str


@dataclass(frozen=True)
class Member:
    obj: "Expr"
    prop: str


@dataclass(frozen=True)
class Subscript:
    # 常量下标访问 `x['k']` / `x[0]`（Spec:77 白名单节点）。index 只允许
    # 字符串或整数常量——使带连字符的 key（如 state.phases['skill-registry-check']）
    # 可表达；成员访问 `x.k` 与 `x['k']` 语义等价。
    obj: "Expr"
    index: object


@dataclass(frozen=True)
class Compare:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class BoolOp:
    op: str  # "and" | "or"
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Not:
    operand: "Expr"


@dataclass(frozen=True)
class Call:
    callee: str
    args: tuple["Expr", ...]


Expr = Literal | ListLit | Ident | Member | Subscript | Compare | BoolOp | Not | Call


def parse_expression(text: str) -> Expr:
    if len(text) > MAX_EXPR_LEN:
        raise DslError(f"expression too long ({len(text)} > {MAX_EXPR_LEN})")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:  # noqa: TRY003
        raise DslError(f"syntax error: {exc.msg}") from exc
    expr = _convert(tree.body, depth=0)
    return expr


def _convert(node: ast.expr, depth: int) -> Expr:
    if depth > MAX_DEPTH:
        raise DslError(f"expression nested too deeply (> {MAX_DEPTH})")
    d = depth + 1

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return Literal(node.value)
        raise DslError(f"unsupported constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in _KEYWORD_LITERALS:
            return Literal(_KEYWORD_LITERALS[node.id])
        return Ident(node.id)

    if isinstance(node, ast.Attribute):
        return Member(_convert(node.value, d), node.attr)

    if isinstance(node, ast.Subscript):
        index_node = node.slice
        if not isinstance(index_node, ast.Constant) or not isinstance(index_node.value, (str, int)) or isinstance(
            index_node.value, bool
        ):
            raise DslError("subscript index must be a string or integer constant")
        return Subscript(_convert(node.value, d), index_node.value)

    if isinstance(node, ast.List):
        elements: list[object] = []
        for el in node.elts:
            lit = _convert(el, d)
            if not isinstance(lit, Literal):
                raise DslError("list literals may only contain literals")
            elements.append(lit.value)
        return ListLit(tuple(elements))

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return Not(_convert(node.operand, d))

    if isinstance(node, ast.BoolOp):
        op = "and" if isinstance(node.op, ast.And) else "or"
        # ast 把 a and b and c 折叠为多元；还原为左结合二元树
        parts = [_convert(v, d) for v in node.values]
        acc = parts[0]
        for right in parts[1:]:
            acc = BoolOp(op, acc, right)
        return acc

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise DslError("chained comparisons are not allowed")
        op_type = type(node.ops[0])
        if op_type not in _COMPARE_OPS:
            raise DslError(f"comparison operator not allowed: {op_type.__name__}")
        return Compare(_COMPARE_OPS[op_type], _convert(node.left, d), _convert(node.comparators[0], d))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise DslError("only bare-name function calls are allowed")
        callee = node.func.id
        if callee not in BUILTIN_ARITY:
            raise DslError(f"unknown function '{callee}' (not in the allow-list)")
        if node.keywords:
            raise DslError(f"function '{callee}' does not accept keyword arguments")
        args = tuple(_convert(a, d) for a in node.args)
        if len(args) != BUILTIN_ARITY[callee]:
            raise DslError(
                f"function '{callee}' expects {BUILTIN_ARITY[callee]} arg(s), got {len(args)}"
            )
        return Call(callee, args)

    raise DslError(f"expression node not allowed: {type(node).__name__}")


def _walk(expr: Expr) -> list[Expr]:
    out: list[Expr] = [expr]
    if isinstance(expr, Member):
        out += _walk(expr.obj)
    elif isinstance(expr, Subscript):
        out += _walk(expr.obj)
    elif isinstance(expr, (Compare, BoolOp)):
        out += _walk(expr.left) + _walk(expr.right)
    elif isinstance(expr, Not):
        out += _walk(expr.operand)
    elif isinstance(expr, Call):
        for a in expr.args:
            out += _walk(a)
    return out


def collect_calls(expr: Expr) -> list[Call]:
    return [n for n in _walk(expr) if isinstance(n, Call)]


def collect_gate_refs(expr: Expr) -> list[str]:
    refs: list[str] = []
    for call in collect_calls(expr):
        if call.callee == "gate" and call.args and isinstance(call.args[0], Literal):
            val = call.args[0].value
            if isinstance(val, str):
                refs.append(val)
    return refs
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_dsl_parse.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/orchestration/__init__.py \
        assurance_agent/workflow/orchestration/dsl.py tests/unit/test_dsl_parse.py
git commit -m "feat: add DSL parser with ast whitelist and builtin arity table"
```

---

### Task 3: DSL 三值求值 + 作用域 + 内建（`workflow/orchestration/dsl.py` 下半）

三值真值表（`T`=true、`F`=false、`M`=MISSING）：

| a | b | a and b | a or b |
|---|---|---------|--------|
| F | * | F | b |
| T | b | b | T |
| M | F | F | M |
| M | T | M | T |
| M | M | M | M |

- `not T=F`、`not F=T`、`not M=M`。
- 比较：任一操作数为 `MISSING` → `MISSING`；`==`/`!=` 为 typed 相等（跨类型不相等；list/dict 深比较；无跨类型 `1 == '1'`）；`< <= > >=` 仅同为 number 或同为 str 才比较，否则 `MISSING`。
- `in` / `not in`：左为 `MISSING` → `MISSING`；右非 list → `MISSING`；否则 typed 相等成员判定。
- 成员访问 `obj.prop`：obj 为 `MISSING`/`None`/非 dict → `MISSING`；dict 缺键 → `MISSING`。
- `len(x)`：str/list/dict 返回长度，否则 `MISSING`。`defined(x)`：`x is not MISSING`（返回 bool）。
- `any/all/count(coll, pred)`：coll 非 list → `MISSING`；对每个元素建 `ChildScope`（元素为 dict 时其字段遮蔽外层），求 pred 的三值：
  - `count`：统计 pred 为 `True` 的个数（int）。
  - `all`：遇 `False` 立即 `False`；否则遇任一 `MISSING` 记账，全程无 `False` 但有 `MISSING` → `MISSING`；空集合 → `True`。
  - `any`：遇 `True` 立即 `True`；否则遇任一 `MISSING` 记账，无 `True` 但有 `MISSING` → `MISSING`；空集合 → `False`。
- `file_exists(p)`：p 非 str → `MISSING`；无解析器 → `DslError`；否则返回 bool。
- `gate(id)`：id 非 str → `MISSING`；无解析器 → `DslError`；否则返回 `{"verdict": <resolver(id)>}`（dict，供 `gate('x').verdict == 'pass'` 访问）。

**Files:**
- Modify: `assurance_agent/workflow/orchestration/dsl.py`
- Test: `tests/unit/test_dsl_eval.py`

**Interfaces:**
- Consumes: Task 2 的 AST 节点。
- Produces:
  - 哨兵 `MISSING`（`_Missing()` 单例，`repr='MISSING'`）。
  - `class Scope`：`Scope(vars: dict[str, object], *, file_exists=None, gate_verdict=None)`；`lookup(name) -> object`（缺失返回 `MISSING`）；`child(element) -> Scope`（元素字段遮蔽 + 继承解析器）。
  - `evaluate(expr: Expr, scope: Scope) -> object`（返回 `True`/`False`/`MISSING`/具体值）。
  - `is_satisfied(expr: Expr, scope: Scope) -> bool`（`evaluate(...) is True`，fail-closed）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_dsl_eval.py
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    Scope,
    evaluate,
    is_satisfied,
    parse_expression,
)


def ev(text, vars_, **kw):
    return evaluate(parse_expression(text), Scope(vars_, **kw))


def test_missing_propagation():
    assert ev("state.x == 'y'", {"state": {}}) is MISSING
    assert ev("not state.x", {"state": {}}) is MISSING
    assert ev("state.x == 'y' and false", {"state": {}}) is False   # F 短路
    assert ev("state.x == 'y' or true", {"state": {}}) is True      # T 短路
    assert ev("state.x == 'y' and true", {"state": {}}) is MISSING


def test_typed_equality():
    assert ev("a == 1", {"a": 1}) is True
    assert ev("a == '1'", {"a": 1}) is False    # 无跨类型相等
    assert ev("a in ['x', 'y']", {"a": "y"}) is True
    assert ev("a in b", {"a": 1, "b": "notalist"}) is MISSING


def test_in_uses_params_list():
    vars_ = {"params": {"test_types": ["api", "e2e"]}}
    assert ev("'api' in params.test_types", vars_) is True
    assert ev("'fuzz' in params.test_types", vars_) is False


def test_any_all_count_child_scope():
    vars_ = {"fix_proposal": {"proposals": [
        {"target": "api", "eligible": True},
        {"target": "e2e", "eligible": False},
    ]}}
    assert ev("any(fix_proposal.proposals, target == 'api' and eligible == true)", vars_) is True
    assert ev("all(fix_proposal.proposals, eligible == true)", vars_) is False
    assert ev("count(fix_proposal.proposals, eligible == true)", vars_) == 1


def test_any_empty_and_missing():
    assert ev("any(xs, eligible == true)", {"xs": []}) is False
    assert ev("all(xs, eligible == true)", {"xs": []}) is True
    assert ev("any(xs, eligible == true)", {"xs": [{}]}) is MISSING  # 元素无 eligible


def test_builtins():
    assert ev("len(a) > 0", {"a": [1, 2]}) is True
    assert ev("defined(a)", {"a": None}) is True     # None 是已定义
    assert ev("defined(a)", {}) is False
    assert ev("file_exists('healing/x.json')", {}, file_exists=lambda p: True) is True
    assert ev("gate('g').verdict == 'enter'", {}, gate_verdict=lambda i: "enter") is True


def test_subscript_eval():
    vars_ = {"state": {"phases": {"skill-registry-check": {"status": "pass"}}}}
    assert ev("state.phases['skill-registry-check'].status == 'pass'", vars_) is True
    # 缺键 → MISSING → fail-closed
    assert ev("state.phases['nope'].status == 'pass'", vars_) is MISSING
    # x['k'] 与 x.k 等价
    assert ev("state['phases']['skill-registry-check']['status'] == 'pass'", vars_) is True


def test_is_satisfied_fail_closed():
    assert is_satisfied(parse_expression("state.x == 'y'"), Scope({"state": {}})) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_dsl_eval.py -v`
Expected: FAIL（`ImportError: cannot import name 'MISSING'`）

- [ ] **Step 3: 追加实现到 `dsl.py`**

```python
# —— 追加到 assurance_agent/workflow/orchestration/dsl.py 末尾 ——
from collections.abc import Callable  # noqa: E402  (放到文件顶部 import 区)


class _Missing:
    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()

FileExistsResolver = Callable[[str], bool]
GateResolver = Callable[[str], str]


class Scope:
    def __init__(
        self,
        vars: dict[str, object],
        *,
        file_exists: FileExistsResolver | None = None,
        gate_verdict: GateResolver | None = None,
    ) -> None:
        self._vars = vars
        self.file_exists = file_exists
        self.gate_verdict = gate_verdict

    def lookup(self, name: str) -> object:
        return self._vars[name] if name in self._vars else MISSING

    def child(self, element: object) -> "Scope":
        base = dict(self._vars)
        if isinstance(element, dict):
            base.update(element)
        return Scope(base, file_exists=self.file_exists, gate_verdict=self.gate_verdict)


def _to_bool(v: object) -> bool | None:
    if v is True:
        return True
    if v is False:
        return False
    return None  # MISSING/其它 → 三值 unknown


def _typed_eq(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    ta = "num" if isinstance(a, (int, float)) else type(a).__name__
    tb = "num" if isinstance(b, (int, float)) else type(b).__name__
    if ta != tb:
        return False
    return a == b


def evaluate(expr: Expr, scope: Scope) -> object:  # noqa: C901 - 语言解释器
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, ListLit):
        return list(expr.elements)
    if isinstance(expr, Ident):
        return scope.lookup(expr.name)
    if isinstance(expr, Member):
        obj = evaluate(expr.obj, scope)
        if not isinstance(obj, dict):
            return MISSING
        return obj[expr.prop] if expr.prop in obj else MISSING
    if isinstance(expr, Subscript):
        obj = evaluate(expr.obj, scope)
        idx = expr.index
        if isinstance(idx, str):
            return obj[idx] if isinstance(obj, dict) and idx in obj else MISSING
        # int 下标：list 越界/负数按缺失（fail-closed），不做 Python 负索引语义
        if isinstance(obj, list) and 0 <= idx < len(obj):
            return obj[idx]
        return MISSING
    if isinstance(expr, Not):
        b = _to_bool(evaluate(expr.operand, scope))
        return MISSING if b is None else (not b)
    if isinstance(expr, BoolOp):
        return _eval_boolop(expr, scope)
    if isinstance(expr, Compare):
        return _eval_compare(expr, scope)
    if isinstance(expr, Call):
        return _eval_call(expr, scope)
    raise DslError(f"cannot evaluate node: {expr!r}")


def _eval_boolop(expr: BoolOp, scope: Scope) -> object:
    a = _to_bool(evaluate(expr.left, scope))
    if expr.op == "and":
        if a is False:
            return False
        b = _to_bool(evaluate(expr.right, scope))
        if a is True:
            return MISSING if b is None else b
        return False if b is False else MISSING  # a is None
    # or
    if a is True:
        return True
    b = _to_bool(evaluate(expr.right, scope))
    if a is False:
        return MISSING if b is None else b
    return True if b is True else MISSING  # a is None


def _eval_compare(expr: Compare, scope: Scope) -> object:
    if expr.op in ("in", "not in"):
        left = evaluate(expr.left, scope)
        right = evaluate(expr.right, scope)
        if left is MISSING or not isinstance(right, list):
            return MISSING
        found = any(_typed_eq(left, el) for el in right)
        return found if expr.op == "in" else (not found)

    left = evaluate(expr.left, scope)
    right = evaluate(expr.right, scope)
    if left is MISSING or right is MISSING:
        return MISSING
    if expr.op == "==":
        return _typed_eq(left, right)
    if expr.op == "!=":
        return not _typed_eq(left, right)
    both_num = isinstance(left, (int, float)) and not isinstance(left, bool) and \
        isinstance(right, (int, float)) and not isinstance(right, bool)
    both_str = isinstance(left, str) and isinstance(right, str)
    if not both_num and not both_str:
        return MISSING
    if expr.op == "<":
        return left < right  # type: ignore[operator]
    if expr.op == "<=":
        return left <= right  # type: ignore[operator]
    if expr.op == ">":
        return left > right  # type: ignore[operator]
    return left >= right  # type: ignore[operator]


def _eval_call(expr: Call, scope: Scope) -> object:
    callee = expr.callee
    if callee == "len":
        v = evaluate(expr.args[0], scope)
        if isinstance(v, (str, list, dict)):
            return len(v)
        return MISSING
    if callee == "defined":
        return evaluate(expr.args[0], scope) is not MISSING
    if callee == "file_exists":
        p = evaluate(expr.args[0], scope)
        if not isinstance(p, str):
            return MISSING
        if scope.file_exists is None:
            raise DslError("file_exists() called but no resolver was provided")
        return scope.file_exists(p)
    if callee == "gate":
        gid = evaluate(expr.args[0], scope)
        if not isinstance(gid, str):
            return MISSING
        if scope.gate_verdict is None:
            raise DslError("gate() called but no resolver was provided")
        return {"verdict": scope.gate_verdict(gid)}
    # any / all / count
    coll = evaluate(expr.args[0], scope)
    pred = expr.args[1]
    if not isinstance(coll, list):
        return MISSING
    if callee == "count":
        return sum(1 for el in coll if _to_bool(evaluate(pred, scope.child(el))) is True)
    saw_missing = False
    if callee == "all":
        for el in coll:
            r = _to_bool(evaluate(pred, scope.child(el)))
            if r is False:
                return False
            if r is None:
                saw_missing = True
        return MISSING if saw_missing else True
    # any
    for el in coll:
        r = _to_bool(evaluate(pred, scope.child(el)))
        if r is True:
            return True
        if r is None:
            saw_missing = True
    return MISSING if saw_missing else False


def is_satisfied(expr: Expr, scope: Scope) -> bool:
    return evaluate(expr, scope) is True
```

- [ ] **Step 4: 把 `Callable` import 移到文件顶部并运行确认通过**

把 Step 3 代码块中的 `from collections.abc import Callable` 放到 `dsl.py` 顶部 import 区（与 `ast` 同段），不得留下 `# noqa: E402`。

Run: `uv run pytest tests/unit/test_dsl_eval.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/orchestration/dsl.py tests/unit/test_dsl_eval.py
git commit -m "feat: add three-valued DSL evaluator with scopes and builtins"
```

---

### Task 4: WorkflowSchema 模型 + 加载 + 静态校验（`workflow/orchestration/schema.py`）

**Files:**
- Create: `assurance_agent/workflow/orchestration/schema.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`（`attempts_used` counter + fixer-safety canonical 顺序）
- Test: `tests/unit/test_schema.py`

**Interfaces:**
- Consumes: `dsl.parse_expression` / `collect_calls` / `collect_gate_refs` / `BUILTIN_ARITY` / `DslError`；`resources.read_text` / `exists`。
- Produces（供 M4/M6 消费）：
  - pydantic 模型 `ParamSpec`、`PhaseDef`、`LoopDef`、`GateRule`、`ReadEntry`、`GateDef`、`WorkflowSchema`。
  - `WorkflowSchema.phase_produces(phase_id) -> list[str] | None`、`has_phase(phase_id) -> bool`、`gate_for_phase(phase_id) -> str | None`（满足 M2 `WorkflowSchemaLike` 协议 + M4 需求）。
  - `WorkflowSchema.default_param_values() -> dict[str, object]`（把每个 param 的 `default` 汇成 dict，供引擎填缺省）。
  - `class SchemaError(AaError)`。
  - `load_workflow_schema(project_root: Path, explicit: Path | None = None) -> WorkflowSchema`——解析顺序：`explicit`（排他，缺失即报错，不回退）→ `<root>/.aa/workflow-schema.yaml` → `<root>/schemas/workflow-schema.yaml` → 包内默认（`resources.read_text("schemas", "workflow-schema.yaml")`）。加载即静态校验，失败抛 `SchemaError`（附全部错误行）。
  - `parse_schema(yaml_text: str) -> WorkflowSchema`（不做文件查找，供测试直接喂 YAML）。

关键规则（对齐源版 `schema.ts`）：
- gate `reads:` 归一化：字符串项 `path` + 派生 alias（basename 去扩展名，非字母数字→`_`）；`{path, as}` 用显式 alias。**`PhaseDef` 没有 `reads` 字段**——phase `when`/`ready_when` 的 evidence 别名由 `WorkflowSchema.produces_alias_map()`（遍历全部 `produces` 的全局反向别名表，对齐源版 `reverseAlias`）提供，例如 `fix_proposal` 来自 `fix-proposal` phase 的 `healing/fix-proposal.json`。
- gate `*_when` 键**按声明顺序**成为 `GateRule`；schema 与 `GateVerdict` 共用唯一 `Verdict(StrEnum)`，未知规则名或容错 verdict 在加载期抛 `SchemaError`。四个安全 verdict 必须按 `needs_fix→needs_human_review→reject→pass` 的相对顺序声明；打包 `fixer-safety-gate` 在本 Task 同步重排。
- 静态校验：phase id 唯一；`requires_mode ∈ {all,any_active}`；phase requires DAG 无环；phase.requires/gate/repair_of/loop/max_attempts_param 引用完整；loop 成员与 phase.loop 双向一致；global produces alias 无碰撞；gate alias 唯一；verdict 闭集 + 安全序；谓词语法/arity/gate 引用；gate 引用图无环。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_schema.py
from pathlib import Path

import pytest

from assurance_agent.workflow.orchestration.schema import (
    SchemaError,
    load_workflow_schema,
    parse_schema,
)

GOOD = """
schema_version: "1"
name: t
params:
  run_mode: { type: enum, values: [full, case-only], default: full }
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: a
    skill: null
    requires: []
    produces: [x.json]
    gate: g
  - id: b
    skill: aa-explore
    agent: aa-doc-author
    requires: [a]
    produces: [y.json]
    when: "params.run_mode == 'full'"
gates:
  g:
    reads: [x.json]
    needs_fix_when: "decision == 'needs_fix'"
    pass_when: "decision == 'pass'"
"""


def test_parse_and_accessors():
    s = parse_schema(GOOD)
    assert s.has_phase("a") is True
    assert s.has_phase("zzz") is False
    assert s.phase_produces("b") == ["y.json"]
    assert s.gate_for_phase("a") == "g"
    assert s.gate_for_phase("b") is None
    assert s.default_param_values()["run_mode"] == "full"
    # 规则按声明顺序（canonical 安全序）：needs_fix 在前
    assert [r.verdict for r in s.gates["g"].rules] == ["needs_fix", "pass"]
    # alias 派生
    assert s.gates["g"].reads[0].alias == "x"


def test_rejects_bad_predicate():
    bad = GOOD.replace("decision == 'pass'", "decision =! 'pass'")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_unknown_gate_ref():
    bad = GOOD.replace("params.run_mode == 'full'", "gate('nope').verdict == 'pass'")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_dangling_requires():
    bad = GOOD.replace("requires: [a]", "requires: [ghost]")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_explicit_missing_is_error(tmp_path: Path):
    with pytest.raises(SchemaError):
        load_workflow_schema(tmp_path, explicit=tmp_path / "nope.yaml")


def test_packaged_default_loads(tmp_path: Path):
    # 空项目根 → 回退到包内默认 schema，且能通过静态校验
    s = load_workflow_schema(tmp_path)
    assert s.has_phase("explore")
    assert "healing" in s.loops
    assert s.gate_for_phase("case-design") == "case-design-gate"


def test_produces_alias_map_covers_fix_proposal():
    # 全局反向别名表覆盖 healing/fix-proposal.json → fix_proposal，
    # 使 api/e2e-codegen-fix 的 when 无需任何 per-phase reads 即可解析。
    s = load_workflow_schema(Path("/nonexistent"))
    amap = s.produces_alias_map()
    assert amap["fix_proposal"] == "healing/fix-proposal.json"
    assert amap["case_review"] == "review/case-review.json"


def test_rejects_unknown_verdict():
    bad = GOOD.replace("needs_fix_when:", "bogus_when:")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_out_of_order_safety_verdicts():
    # pass 在 needs_fix 之前声明 → 违反 canonical 安全序
    bad = """
schema_version: "1"
name: t
phases:
  - id: a
    skill: null
    requires: []
    produces: [x.json]
    gate: g
gates:
  g:
    reads: [x.json]
    pass_when: "decision == 'pass'"
    needs_fix_when: "decision == 'needs_fix'"
"""
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_duplicate_phase_ids():
    bad = GOOD.replace("  - id: b", "  - id: a")
    with pytest.raises(SchemaError, match="duplicate phase id"):
        parse_schema(bad)


def test_rejects_phase_dependency_cycle():
    bad = GOOD.replace("requires: []", "requires: [b]", 1)
    with pytest.raises(SchemaError, match="phase dependency cycle"):
        parse_schema(bad)


def test_rejects_legacy_phase_reads():
    bad = GOOD.replace("    produces: [y.json]", "    produces: [y.json]\n    reads: [x.json]")
    with pytest.raises(SchemaError, match="phase 'b'.reads"):
        parse_schema(bad)


def test_packaged_healing_contract_is_canonical(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    assert schema.loops["healing"].counter == "state.phases.healing.attempts_used"
    rules = [r.verdict.value for r in schema.gates["fixer-safety-gate"].rules]
    assert rules == ["needs_human_review", "pass"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3a: 定义 schema pydantic 模型、唯一 `Verdict(StrEnum)` 与 YAML normalization；运行 `uv run pytest tests/unit/test_schema.py -v -k 'parse_and_accessors or unknown_verdict or legacy_phase_reads'`**

- [ ] **Step 3b: 实现 packaged/project/explicit 加载顺序与 accessors；运行 `uv run pytest tests/unit/test_schema.py -v -k 'explicit_missing or packaged_default or produces_alias'`**

- [ ] **Step 3c: 实现 phase/loop/alias/requires 静态校验和 phase DAG cycle；运行 `uv run pytest tests/unit/test_schema.py -v -k 'duplicate or dependency_cycle or dangling'`**

- [ ] **Step 3d: 实现 predicate arity/gate 引用/cycle 与 canonical safety-order 校验，同步修复 packaged schema counter/order；运行完整 schema 测试**

以下代码块是 Steps 3a–3d 完成后的 `schema.py` 参考结果；每个切片须先使对应测试转绿再进入下一片：

```python
# assurance_agent/workflow/orchestration/schema.py
"""workflow-schema.yaml 加载器 + 静态校验 + 类型化模型。"""
from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from assurance_agent import resources
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    DslError,
    collect_calls,
    collect_gate_refs,
    parse_expression,
)

ALLOWED_AGENTS = {"aa-doc-author", "aa-test-author", "aa-reviewer", "aa-reporter", "aa-archiver"}
ORCHESTRATOR_INTERNAL = {"skill-registry-check"}
ALLOWED_OWNED_BY = {"full", "intake", "execute"}
_WHEN_SUFFIX = "_when"

class Verdict(StrEnum):
    PASS = "pass"
    NEEDS_FIX = "needs_fix"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECT = "reject"
    STOP = "stop"
    ENTER = "enter"
    SKIP = "skip"
    CONTINUE = "continue"
    EXIT = "exit"


KNOWN_VERDICTS = frozenset(Verdict)
# 四态安全裁决的 canonical 声明顺序（Spec:80）。求值仍按声明顺序 first-true-wins
# （对齐源版 engine.ts adjudicate），但加载期强制这四个 verdict 若同时出现必须按此序声明，
# 使「声明顺序 == 安全优先级」不可被作者颠覆。
_SAFETY_ORDER = (
    Verdict.NEEDS_FIX,
    Verdict.NEEDS_HUMAN_REVIEW,
    Verdict.REJECT,
    Verdict.PASS,
)


class SchemaError(AaError):
    """schema 结构非法或静态校验失败。"""


class ParamSpec(BaseModel):
    type: str
    values: list[object] | None = None
    default: object = None


class PhaseDef(BaseModel):
    id: str
    skill: str | None = None
    requires: list[str] = Field(default_factory=list)
    requires_mode: str = "all"
    produces: list[str] = Field(default_factory=list)
    owned_by: list[str] | None = None
    gate: str | None = None
    when: str | None = None
    ready_when: str | None = None
    loop: str | None = None
    repair_of: str | None = None
    max_attempts_param: str | None = None
    agent: str | None = None


class LoopDef(BaseModel):
    id: str
    members: list[str] = Field(default_factory=list)
    counter: str = ""
    max_param: str = ""
    allocate_on: str = ""
    exit_gate: str = ""


class GateRule(BaseModel):
    field: str
    verdict: Verdict
    expr: str


class ReadEntry(BaseModel):
    path: str
    alias: str


class GateDef(BaseModel):
    id: str
    reads: list[ReadEntry] = Field(default_factory=list)
    rules: list[GateRule] = Field(default_factory=list)
    invalid_json: Verdict | None = None
    missing_field_is: Verdict | None = None
    missing_file_is: Verdict | None = None
    default: Verdict = Verdict.STOP


class WorkflowSchema(BaseModel):
    schema_version: str = ""
    name: str = ""
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    phases: list[PhaseDef] = Field(default_factory=list)
    loops: dict[str, LoopDef] = Field(default_factory=dict)
    gates: dict[str, GateDef] = Field(default_factory=dict)

    def _phase(self, phase_id: str) -> PhaseDef | None:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    def has_phase(self, phase_id: str) -> bool:
        return self._phase(phase_id) is not None

    def phase_produces(self, phase_id: str) -> list[str] | None:
        p = self._phase(phase_id)
        return list(p.produces) if p else None

    def gate_for_phase(self, phase_id: str) -> str | None:
        p = self._phase(phase_id)
        return p.gate if p else None

    def default_param_values(self) -> dict[str, object]:
        return {name: spec.default for name, spec in self.params.items()}

    def produces_alias_map(self) -> dict[str, str]:
        """全局反向别名表：对齐源版 engine.ts 的 reverseAlias。

        遍历**所有** phase 的 `produces`，`derive_alias` 后取首见者，得到
        alias→canonical path。这是 phase `when` / `ready_when` / loop `allocate_on`
        求值时装载 evidence 别名（如 `fix_proposal`）的唯一来源——不再依赖任何
        per-phase `reads` 字段。
        """
        out: dict[str, str] = {}
        for phase in self.phases:
            for produced in phase.produces:
                alias = derive_alias(produced)
                if alias:  # `qa/archive/<change-id>/` 目录 produces 不生成 evidence alias
                    out.setdefault(alias, produced)
        return out


def derive_alias(file_path: str) -> str:
    base = file_path.split("/")[-1]
    no_ext = re.sub(r"\.[^.]+$", "", base)
    return re.sub(r"[^A-Za-z0-9]", "_", no_ext)


def _normalize_reads(raw: object) -> list[ReadEntry]:
    if not isinstance(raw, list):
        return []
    out: list[ReadEntry] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(ReadEntry(path=entry, alias=derive_alias(entry)))
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            path = entry["path"]
            alias = entry["as"] if isinstance(entry.get("as"), str) else derive_alias(path)
            out.append(ReadEntry(path=path, alias=alias))
        else:
            raise SchemaError(f"invalid reads entry: {entry!r}")
    return out


def _normalize_gate(gate_id: str, raw: dict[str, object]) -> GateDef:
    rules: list[GateRule] = []
    for key, val in raw.items():  # dict 保留 YAML 声明顺序
        if key.endswith(_WHEN_SUFFIX):
            try:
                verdict = Verdict(key[: -len(_WHEN_SUFFIX)])
            except ValueError as exc:
                raise SchemaError(f"gate '{gate_id}' unknown verdict in '{key}'") from exc
            rules.append(GateRule(field=key, verdict=verdict, expr=str(val).strip()))

    def optional_verdict(field: str) -> Verdict | None:
        value = raw.get(field)
        if value is None:
            return None
        try:
            return Verdict(str(value))
        except ValueError as exc:
            raise SchemaError(f"gate '{gate_id}' {field} unknown verdict '{value}'") from exc

    return GateDef(
        id=gate_id,
        reads=_normalize_reads(raw.get("reads")),
        rules=rules,
        invalid_json=optional_verdict("invalid_json"),
        missing_field_is=optional_verdict("missing_field_is"),
        missing_file_is=optional_verdict("missing_file_is"),
        default=optional_verdict("default") or Verdict.STOP,
    )


def parse_schema(yaml_text: str) -> WorkflowSchema:
    try:
        doc = yaml.safe_load(yaml_text)
        if not isinstance(doc, dict):
            raise SchemaError("schema root is not a mapping")

        params = {k: ParamSpec(**v) for k, v in (doc.get("params") or {}).items()}
        phases = [PhaseDef(**_coerce_phase(p)) for p in (doc.get("phases") or [])]
        loops = {
            lid: LoopDef(id=lid, **{k: v for k, v in (lv or {}).items()})
            for lid, lv in (doc.get("loops") or {}).items()
        }
        gates = {
            gid: _normalize_gate(gid, gv or {})
            for gid, gv in (doc.get("gates") or {}).items()
        }
        schema = WorkflowSchema(
            schema_version=str(doc.get("schema_version", "")),
            name=str(doc.get("name", "")),
            params=params,
            phases=phases,
            loops=loops,
            gates=gates,
        )
    except SchemaError:
        raise
    except (AttributeError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise SchemaError(f"invalid workflow schema: {exc}") from exc
    _validate(schema)
    return schema


def _coerce_phase(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise SchemaError(f"phase is not a mapping: {raw!r}")
    if not isinstance(raw.get("id"), str):
        raise SchemaError(f"phase missing string id: {raw!r}")
    if "reads" in raw:
        raise SchemaError(f"phase '{raw['id']}'.reads is not supported; use global produces aliases")
    for key in ("when", "ready_when"):
        if isinstance(raw.get(key), str):
            raw[key] = raw[key].strip()  # type: ignore[index]
    return raw


def _all_predicates(schema: WorkflowSchema) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in schema.phases:
        if p.when:
            out.append((f"phase '{p.id}'.when", p.when))
        if p.ready_when:
            out.append((f"phase '{p.id}'.ready_when", p.ready_when))
    for loop in schema.loops.values():
        if loop.allocate_on:
            out.append((f"loop '{loop.id}'.allocate_on", loop.allocate_on))
    for g in schema.gates.values():
        for r in g.rules:
            out.append((f"gate '{g.id}'.{r.field}", r.expr))
    return out


def _detect_gate_cycle(schema: WorkflowSchema) -> list[str]:
    edges: dict[str, list[str]] = {}
    for g in schema.gates.values():
        refs: set[str] = set()
        for r in g.rules:
            try:
                refs.update(collect_gate_refs(parse_expression(r.expr)))
            except DslError:
                pass
        edges[g.id] = [x for x in refs if x in schema.gates]
    errors: list[str] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {gid: WHITE for gid in edges}
    stack: list[str] = []

    def dfs(node: str) -> bool:
        color[node] = GREY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color[nxt] == GREY:
                cycle = stack[stack.index(nxt):] + [nxt]
                errors.append("gate reference cycle: " + " -> ".join(cycle))
                return True
            if color[nxt] == WHITE and dfs(nxt):
                return True
        stack.pop()
        color[node] = BLACK
        return False

    for gid in edges:
        if color[gid] == WHITE and dfs(gid):
            break
    return errors


def _detect_phase_cycle(schema: WorkflowSchema) -> list[str]:
    edges = {p.id: list(p.requires) for p in schema.phases}
    white, grey, black = 0, 1, 2
    color = {pid: white for pid in edges}
    stack: list[str] = []
    errors: list[str] = []

    def dfs(node: str) -> bool:
        color[node] = grey
        stack.append(node)
        for dep in edges[node]:
            if dep not in color:
                continue
            if color[dep] == grey:
                cycle = stack[stack.index(dep):] + [dep]
                errors.append("phase dependency cycle: " + " -> ".join(cycle))
                return True
            if color[dep] == white and dfs(dep):
                return True
        stack.pop()
        color[node] = black
        return False

    for pid in edges:
        if color[pid] == white and dfs(pid):
            break
    return errors


def _validate(schema: WorkflowSchema) -> None:
    errors: list[str] = []
    phase_id_list = [p.id for p in schema.phases]
    phase_ids = set(phase_id_list)
    gate_ids = set(schema.gates)
    param_names = set(schema.params)

    duplicates = sorted(pid for pid in phase_ids if phase_id_list.count(pid) > 1)
    errors.extend(f"duplicate phase id '{pid}'" for pid in duplicates)

    for p in schema.phases:
        if p.requires_mode not in {"all", "any_active"}:
            errors.append(f"phase '{p.id}' invalid requires_mode '{p.requires_mode}'")
        for dep in p.requires:
            if dep not in phase_ids:
                errors.append(f"phase '{p.id}' requires unknown phase '{dep}'")
        if p.gate and p.gate not in gate_ids:
            errors.append(f"phase '{p.id}' references unknown gate '{p.gate}'")
        if p.repair_of and p.repair_of not in phase_ids:
            errors.append(f"phase '{p.id}' repair_of unknown phase '{p.repair_of}'")
        if p.max_attempts_param and p.max_attempts_param not in param_names:
            errors.append(f"phase '{p.id}' max_attempts_param unknown param '{p.max_attempts_param}'")
        if p.loop and p.loop not in schema.loops:
            errors.append(f"phase '{p.id}' references unknown loop '{p.loop}'")
        for scope in p.owned_by or []:
            if scope not in ALLOWED_OWNED_BY:
                errors.append(f"phase '{p.id}' owned_by scope '{scope}' not allowed")
        if p.id not in ORCHESTRATOR_INTERNAL:
            if p.skill is None and p.agent:
                errors.append(f"CLI phase '{p.id}' (skill: null) must not have an agent")
            if p.skill is not None:
                if not p.agent:
                    errors.append(f"agent phase '{p.id}' has a skill but no agent")
                elif p.agent not in ALLOWED_AGENTS:
                    errors.append(f"phase '{p.id}' agent '{p.agent}' not allowed")

    for loop in schema.loops.values():
        for m in loop.members:
            if m not in phase_ids:
                errors.append(f"loop '{loop.id}' member unknown phase '{m}'")
        if loop.exit_gate and loop.exit_gate not in gate_ids:
            errors.append(f"loop '{loop.id}' exit_gate unknown gate '{loop.exit_gate}'")
        if loop.max_param and loop.max_param not in param_names:
            errors.append(f"loop '{loop.id}' max_param unknown param '{loop.max_param}'")
        declared_members = {p.id for p in schema.phases if p.loop == loop.id}
        if declared_members != set(loop.members):
            errors.append(
                f"loop '{loop.id}' members disagree with phase.loop: "
                f"loop={sorted(loop.members)} phases={sorted(declared_members)}"
            )

    produced_aliases: dict[str, str] = {}
    for phase in schema.phases:
        for path in phase.produces:
            alias = derive_alias(path)
            if not alias:
                continue  # directory-only archive produce is not evidence-addressable
            if alias in produced_aliases and produced_aliases[alias] != path:
                errors.append(
                    f"global produces alias collision '{alias}': "
                    f"'{produced_aliases[alias]}' vs '{path}'"
                )
            produced_aliases[alias] = path

    for g in schema.gates.values():
        seen: dict[str, str] = {}
        for r in g.reads:
            if r.alias in seen and seen[r.alias] != r.path:
                errors.append(f"gate '{g.id}' alias collision '{r.alias}'")
            seen[r.alias] = r.path
        # Verdict 已在 normalization 时转成唯一 StrEnum；未知值无法进入模型。
        # 四态安全顺序：needs_fix→needs_human_review→reject→pass 若同时出现须按此序声明。
        present = [v for v in _SAFETY_ORDER if any(r.verdict == v for r in g.rules)]
        declared = [r.verdict for r in g.rules if r.verdict in _SAFETY_ORDER]
        if declared != present:
            errors.append(
                f"gate '{g.id}' safety verdicts must be declared in canonical order "
                f"{present}, got {declared}"
            )

    for loc, expr in _all_predicates(schema):
        try:
            ast_node = parse_expression(expr)
        except DslError as exc:
            errors.append(f"{loc}: invalid predicate — {exc}")
            continue
        for call in collect_calls(ast_node):
            expected = BUILTIN_ARITY.get(call.callee)
            if expected is None:
                errors.append(f"{loc}: unknown function '{call.callee}'")
            elif len(call.args) != expected:
                errors.append(f"{loc}: '{call.callee}' expects {expected} arg(s), got {len(call.args)}")
        for gid in collect_gate_refs(ast_node):
            if gid not in gate_ids:
                errors.append(f"{loc}: references unknown gate '{gid}'")

    errors.extend(_detect_phase_cycle(schema))
    errors.extend(_detect_gate_cycle(schema))

    if errors:
        raise SchemaError("schema validation failed:\n  - " + "\n  - ".join(errors))


def load_workflow_schema(project_root: Path, explicit: Path | None = None) -> WorkflowSchema:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else project_root / explicit
        if not path.exists():
            raise SchemaError(f"explicit schema not found: {path}")
        return parse_schema(path.read_text(encoding="utf-8"))
    for rel in (Path(".aa/workflow-schema.yaml"), Path("schemas/workflow-schema.yaml")):
        candidate = project_root / rel
        if candidate.exists():
            return parse_schema(candidate.read_text(encoding="utf-8"))
    return parse_schema(resources.read_text("schemas", "workflow-schema.yaml"))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/orchestration/schema.py assurance_agent/_resources/schemas/workflow-schema.yaml tests/unit/test_schema.py
git commit -m "feat: add workflow schema model, loader and static validation"
```

---

### Task 5: workflow-state 读写 + 完整性校验（`workflow/core/state.py`）

**Files:**
- Create: `assurance_agent/workflow/core/state.py`
- Test: `tests/unit/test_state.py`

**Interfaces:**
- Consumes: M2 canonical `WorkflowState` 及显式 `WorkflowPhases` / `HealingPhaseState` / `WorkflowGates`。`read_state` 与 `write_state` 只交换这一类型；已知核心字段使用 `state.phases.healing.attempts_used` 等属性访问，动态 phase 才落入 `model_extra`。顶层 `state.healing` 与裸 dict state 均为非法合同。
- Produces（供 M4/M6 消费）：
  - `WORKFLOW_STATE_RELPATH = "workflow-state.yaml"`；`state_file(change_dir: Path) -> Path`。
  - `read_state(change_dir: Path) -> WorkflowState`——文件不存在返回 `WorkflowState()`（空态）；存在但完整性哈希不符抛 `StateIntegrityError`（哈希缺失时视为遗留态，容忍并在下次写入补齐）。文件级元数据键 `_integrity` 在校验后剥离，不进入模型字段（pydantic v2 保留下划线名，不能作为 field/extra，必须显式剥离）。
  - `write_state(change_dir: Path, state: WorkflowState) -> None`——原子写（临时文件 + `os.replace`）；序列化 `state.model_dump(mode="json")`（含 `extra="allow"` 的扩展键如 `gates`；healing 位于 `phases.healing`），最后写入 `_integrity.state_sha256`（对去掉 `_integrity` 后的 canonical JSON）。
  - `state_guard(change_dir: Path) -> str`——磁盘 state 文件的 sha256（不存在返回空串哨兵）；对齐源版 `computeStateGuard`，供 M6 dispatch 信封做 H0「dispatch 后 state 未变」守卫。
  - `class StateIntegrityError(AaError)`。

> **写边界与审计一致性（re-baseline，对齐源版）**：M3 只提供 `read_state`/`write_state`（原子）+ `append_event_*`（Task 6）这些原语。**不再引入 `_txn` / WAL 两阶段提交**。「先落 strict 审计事件、后推进 state；两步之间失败以事件为准回滚 state」（Spec:88/89）由 **M6 `progression`** 的写边界统一实现——对齐源版 `progression.ts`：写前用文件快照 `capture(state, events)`，失败即 `restore`；并用**幂等标记**保证重放安全（`phase_outcome_committed.attempt_id`、`healing_attempt_allocated.operation_id`、`dispatch_signed.state_guard`）。M3 冻结这些事件形状（Task 6），不实现事务组合。

> 设计说明（`versioned` 级产物）：完整性哈希用于探测 Subagent 直接篡改 workflow-state.yaml（对齐源版「防篡改」意图，clean-room 机制）。`_integrity` 是保留顶层元数据键，不进入 DSL 作用域（引擎构造 scope 时用 `model_dump()`，天然只含模型字段+extra，不含该元数据）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_state.py
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import PhaseState, WorkflowState
from assurance_agent.workflow.core.state import (
    StateIntegrityError,
    read_state,
    state_guard,
    write_state,
)


def test_roundtrip_returns_workflowstate(tmp_path: Path):
    write_state(tmp_path, WorkflowState(phases={"explore": {"status": "done"}}))
    got = read_state(tmp_path)
    assert isinstance(got, WorkflowState)
    assert got.phases.model_extra is not None
    assert got.phases.model_extra["explore"]["status"] == "done"
    # 元数据键不进入模型
    assert "_integrity" not in (got.model_extra or {})


def test_known_state_fields_are_typed_and_roundtrip(tmp_path: Path):
    st = read_state(tmp_path)            # 空态
    st.phases.skill_registry_check = PhaseState(status="pass")
    st.phases.healing.status = "resolved"
    st.phases.healing.attempts_used = 1
    st.gates.healing_available = True
    write_state(tmp_path, st)
    back = read_state(tmp_path)
    assert back.phases.skill_registry_check is not None
    assert back.phases.skill_registry_check.status == "pass"
    assert back.phases.healing.status == "resolved"
    assert back.phases.healing.attempts_used == 1
    assert back.gates.healing_available is True


def test_missing_returns_empty_model(tmp_path: Path):
    st = read_state(tmp_path)
    assert isinstance(st, WorkflowState)
    assert st.phases.healing.attempts_used == 0


def test_tamper_detected(tmp_path: Path):
    write_state(tmp_path, WorkflowState(phases={"a": {"status": "done"}}))
    f = tmp_path / "workflow-state.yaml"
    doc = yaml.safe_load(f.read_text())
    doc["phases"]["a"]["status"] = "FORGED"   # 篡改但不更新哈希
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(StateIntegrityError):
        read_state(tmp_path)


def test_legacy_without_hash_tolerated(tmp_path: Path):
    f = tmp_path / "workflow-state.yaml"
    f.write_text(yaml.safe_dump({"phases": {"a": {"status": "done"}}}))  # 无 _integrity
    got = read_state(tmp_path)
    assert got.phases.model_extra is not None
    assert got.phases.model_extra["a"]["status"] == "done"


def test_state_guard_changes_with_content(tmp_path: Path):
    write_state(tmp_path, WorkflowState(phases={"a": {"status": "done"}}))
    g1 = state_guard(tmp_path)
    write_state(tmp_path, WorkflowState(phases={"a": {"status": "PASS"}}))
    g2 = state_guard(tmp_path)
    assert g1 and g2 and g1 != g2   # H0 守卫：state 变化后 guard 必变
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: FAIL（`ModuleNotFoundError` / `ImportError`）

- [ ] **Step 3: 写实现**

```python
# assurance_agent/workflow/core/state.py
"""workflow-state.yaml 原子读写 + 完整性哈希（防篡改）。

read_state/write_state 以 M2 canonical `WorkflowState`（extra='allow'）为交换类型，
不返回裸 dict——这样 M4/M6 的属性读写与引擎的 model_dump() 作用域构造完全一致。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError

WORKFLOW_STATE_RELPATH = "workflow-state.yaml"
_INTEGRITY_KEY = "_integrity"


class StateIntegrityError(AaError):
    """workflow-state.yaml 完整性哈希不匹配（疑似被篡改）。"""


def state_file(change_dir: Path) -> Path:
    return change_dir / WORKFLOW_STATE_RELPATH


def _canonical_hash(doc: dict[str, object]) -> str:
    payload = {k: v for k, v in doc.items() if k != _INTEGRITY_KEY}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_doc(change_dir: Path) -> dict[str, object] | None:
    file = state_file(change_dir)
    if not file.exists():
        return None
    doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise StateIntegrityError(f"workflow-state is not a mapping: {file}")
    return doc


def read_state(change_dir: Path) -> WorkflowState:
    doc = _load_doc(change_dir)
    if doc is None:
        return WorkflowState()
    integrity = doc.get(_INTEGRITY_KEY)
    if isinstance(integrity, dict) and isinstance(integrity.get("state_sha256"), str):
        if _canonical_hash(doc) != integrity["state_sha256"]:
            raise StateIntegrityError(
                f"workflow-state integrity check failed: {state_file(change_dir)}"
            )
    # 剥离文件级元数据（pydantic v2 不接受下划线前缀键作 field/extra）。
    fields = {k: v for k, v in doc.items() if k != _INTEGRITY_KEY}
    return WorkflowState.model_validate(fields)


def write_state(change_dir: Path, state: WorkflowState) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = state.model_dump(mode="json")
    doc[_INTEGRITY_KEY] = {"state_sha256": _canonical_hash(doc)}
    file = state_file(change_dir)
    tmp = file.with_suffix(file.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, file)  # 原子替换：磁盘 state 永不半写


def state_guard(change_dir: Path) -> str:
    """磁盘 state 文件 sha256（对齐源版 computeStateGuard）；文件缺失返回空串哨兵。"""
    file = state_file(change_dir)
    if not file.exists():
        return ""
    return hashlib.sha256(file.read_bytes()).hexdigest()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: PASS（6 passed）

> pyright 提示：`state.py` import M2 `WorkflowState` 属 workflow → artifacts 依赖，符合冻结后的 import 契约（`workflow` 层在 `artifacts` 之上，见系列文档「import 契约（冻结）」）。若实现 M3 时 M2 尚未落地 `WorkflowState`，**先落 M2 Task 3**（M2 是 M3 的前置里程碑），不再提供 M3 内联占位。

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/core/state.py tests/unit/test_state.py
git commit -m "feat: add atomic workflow-state read/write with integrity hashing"
```

---

### Task 6A: typed audit events + events.jsonl 双模式写入（`workflow/core/events.py`）

**Files:**
- Create: `assurance_agent/workflow/core/events.py`
- Test: `tests/unit/test_events.py`

**Interfaces:**
- Consumes: 无（纯 append-only + 读；不 import `state`，避免 core 内部环）。
- Produces（供 M4/M5/M6 消费）：
  - `EVENTS_RELPATH = "events.jsonl"`。
  - discriminated pydantic union `AuditEvent`：`HumanDecisionEvent | DispatchSignedEvent | PhaseOutcomeCommittedEvent | HealingAttemptAllocatedEvent | HealRecordApplyEvent | HealTransitionEvent | HealingEntryBaselinePinnedEvent`，每个模型 `extra="forbid"`。
  - `append_event_best_effort(change_dir: Path, event: Mapping[str, object]) -> None`——遥测型；捕获 JSON 序列化、读取旧日志和 IO 的全部普通异常，绝不影响调用者退出码。
  - `append_event_strict(change_dir: Path, event: AuditEvent | Mapping[str, object]) -> None`——先用 `TypeAdapter(AuditEvent)` 校验，再 append；结构或写入失败统一抛 `EventWriteError`。
  - `read_events(change_dir: Path) -> list[dict[str, object]]`——只保留 JSON object；坏 JSON、scalar、array 均跳过。
  - `next_seq(change_dir: Path) -> int`——`max(seq)+1`（缺省 1）。
  - `class EventWriteError(AaError)`。
  - 每条事件自动补 `seq`（递增）、`ts`（ISO8601 UTC）。

**冻结事件形状契约**——下表由上述 pydantic union 执行，不再只是 prose。M4/M5/M6 必须构造这些模型或提交可通过同一 adapter 的 mapping：

| type | source | 关键字段 | 幂等/审计键 |
|---|---|---|---|
| `human_decision` | decide | checkpoint, action, reason, who, review_file?, review_sha256? | review_sha256 |
| `dispatch_signed` | progression | phase, kind, target?, attempt_id, state_guard, dispatched_at | attempt_id + state_guard |
| `phase_outcome_committed` | progression | phase, attempt_id, gate_report | attempt_id |
| `healing_attempt_allocated` | progression | episode_id, attempt_id, attempt_number, operation_id, source_batch_id | operation_id |
| `heal_record_apply` | heal | target, proposal_sha256, source_batch_id, attempt_key, summary_sha256, files_modified | attempt_key = `"{proposal_sha256}:{source_batch_id}"` |
| `heal_transition` | status | from, to, source_batch_id?, proposal_sha256?, attempt_key? | — |
| `healing_entry_baseline_pinned` | heal | artifact_file, artifact_sha256, entry_batch_id, episode_id | — |

> **唯一 attempt 事实源**：`attempts_used` 只数当前 episode 的 `healing_attempt_allocated`，按 `operation_id` 去重；`heal_record_apply` 只说明 target apply 结果。allocation 已落而 apply 失败仍消耗预算。Task 6B 冻结 snapshot 原语，M6 组合这些原语但不得改签名或重新发明 WAL。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_events.py
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import (
    EventWriteError,
    HealingAttemptAllocatedEvent,
    append_event_best_effort,
    append_event_strict,
    next_seq,
    read_events,
)


def _allocation(operation_id: str = "op-1") -> HealingAttemptAllocatedEvent:
    return HealingAttemptAllocatedEvent(
        episode_id="episode-1", attempt_id="attempt-1", attempt_number=1,
        operation_id=operation_id, source_batch_id="batch-1",
    )


def test_strict_appends_seq_and_ts(tmp_path: Path):
    append_event_strict(tmp_path, _allocation("op-1"))
    append_event_strict(tmp_path, _allocation("op-2"))
    evs = read_events(tmp_path)
    assert [e["seq"] for e in evs] == [1, 2]
    assert all("ts" in e for e in evs)
    assert next_seq(tmp_path) == 3


def test_strict_fails_when_dir_missing(tmp_path: Path):
    missing = tmp_path / "no-such-change"
    with pytest.raises(EventWriteError):
        append_event_strict(missing, _allocation())


def test_strict_rejects_missing_idempotency_key(tmp_path: Path):
    with pytest.raises(EventWriteError, match="operation_id"):
        append_event_strict(tmp_path, {
            "source": "progression", "type": "healing_attempt_allocated",
            "episode_id": "e", "attempt_id": "a", "attempt_number": 1,
            "source_batch_id": "b",
        })


def test_best_effort_swallows_error(tmp_path: Path, capsys):
    missing = tmp_path / "no-such-change"
    append_event_best_effort(missing, {"type": "status_query"})  # 不抛
    assert read_events(missing) == []
    append_event_best_effort(tmp_path, {"type": "status_query", "bad": {1, 2}})  # TypeError 亦不抛


def test_read_skips_corrupt_and_non_object_lines(tmp_path: Path):
    append_event_strict(tmp_path, _allocation())
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write("42\n")
        fh.write("[1, 2]\n")
    evs = read_events(tmp_path)
    assert [e["type"] for e in evs] == ["healing_attempt_allocated"]
    assert next_seq(tmp_path) == 2


AUDIT_FIXTURES = [
    {"source": "decide", "type": "human_decision", "checkpoint": "g", "action": "stop", "reason": "r", "who": "u"},
    {"source": "progression", "type": "dispatch_signed", "phase": "inspect", "kind": "dispatch_phase", "attempt_id": "a", "state_guard": "s", "dispatched_at": 1},
    {"source": "progression", "type": "phase_outcome_committed", "phase": "inspect", "attempt_id": "a", "gate_report": None},
    {"source": "progression", "type": "healing_attempt_allocated", "episode_id": "e", "attempt_id": "ha", "attempt_number": 1, "operation_id": "op", "source_batch_id": "b"},
    {"source": "heal", "type": "heal_record_apply", "target": "api", "proposal_sha256": "p", "source_batch_id": "b", "attempt_key": "p:b", "summary_sha256": "s", "files_modified": []},
    {"source": "status", "type": "heal_transition", "from": "pending", "to": "failed"},
    {"source": "heal", "type": "healing_entry_baseline_pinned", "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x", "entry_batch_id": "b", "episode_id": "e"},
]


@pytest.mark.parametrize("payload", AUDIT_FIXTURES)
def test_every_frozen_audit_shape_serializes(tmp_path: Path, payload: dict):
    append_event_strict(tmp_path, payload)
    assert read_events(tmp_path)[0]["type"] == payload["type"]


def test_events_are_jsonl(tmp_path: Path):
    append_event_strict(tmp_path, _allocation())
    line = (tmp_path / "events.jsonl").read_text().strip()
    assert json.loads(line)["operation_id"] == "op-1"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 写实现**

```python
# assurance_agent/workflow/core/events.py
"""events.jsonl append-only 写入：遥测型 best-effort + 审计型 strict（对齐源版 events.ts）。

M3 只提供 append 原语 + 读取；事务性写边界（先事件后 state、失败回滚）在 M6 progression
以文件快照 + 幂等标记实现。core 内不 import state，避免层内环。
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assurance_agent.exceptions import AaError

EVENTS_RELPATH = "events.jsonl"


class _AuditEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HumanDecisionEvent(_AuditEventBase):
    source: Literal["decide"] = "decide"
    type: Literal["human_decision"] = "human_decision"
    checkpoint: str
    action: Literal["fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"]
    reason: str
    who: str
    review_file: str | None = None
    review_sha256: str | None = None


class DispatchSignedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["dispatch_signed"] = "dispatch_signed"
    phase: str
    kind: Literal["dispatch_phase", "heal"]
    target: Literal["api", "e2e"] | None = None
    attempt_id: str
    state_guard: str
    dispatched_at: int


class PhaseOutcomeCommittedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["phase_outcome_committed"] = "phase_outcome_committed"
    phase: str
    attempt_id: str
    gate_report: dict[str, object] | None


class HealingAttemptAllocatedEvent(_AuditEventBase):
    source: Literal["progression"] = "progression"
    type: Literal["healing_attempt_allocated"] = "healing_attempt_allocated"
    episode_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str


class HealRecordApplyEvent(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["heal_record_apply"] = "heal_record_apply"
    target: Literal["api", "e2e"]
    proposal_sha256: str
    source_batch_id: str
    attempt_key: str
    summary_sha256: str | None
    files_modified: list[str]


class HealTransitionEvent(_AuditEventBase):
    source: Literal["status"] = "status"
    type: Literal["heal_transition"] = "heal_transition"
    from_: str = Field(alias="from")
    to: str
    source_batch_id: str | None = None
    proposal_sha256: str | None = None
    attempt_key: str | None = None


class HealingEntryBaselinePinnedEvent(_AuditEventBase):
    source: Literal["heal"] = "heal"
    type: Literal["healing_entry_baseline_pinned"] = "healing_entry_baseline_pinned"
    artifact_file: Literal["healing/entry-baseline.json"]
    artifact_sha256: str
    entry_batch_id: str
    episode_id: str


AuditEvent = Annotated[
    HumanDecisionEvent | DispatchSignedEvent | PhaseOutcomeCommittedEvent
    | HealingAttemptAllocatedEvent | HealRecordApplyEvent | HealTransitionEvent
    | HealingEntryBaselinePinnedEvent,
    Field(discriminator="type"),
]
_AUDIT_ADAPTER = TypeAdapter(AuditEvent)


class EventWriteError(AaError):
    """strict 审计事件写入失败。"""


def _events_file(change_dir: Path) -> Path:
    return change_dir / EVENTS_RELPATH


def read_events(change_dir: Path) -> list[dict[str, object]]:
    file = _events_file(change_dir)
    if not file.exists():
        return []
    out: list[dict[str, object]] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue  # 容错：跳过坏行（对齐源版 readEvents）
        if isinstance(value, dict):
            out.append(value)
    return out


def next_seq(change_dir: Path) -> int:
    seqs = [e["seq"] for e in read_events(change_dir) if isinstance(e.get("seq"), int)]
    return (max(seqs) + 1) if seqs else 1


def _append(change_dir: Path, event: Mapping[str, object]) -> None:
    if not change_dir.exists():
        raise EventWriteError(f"change directory does not exist: {change_dir}")
    record = {
        "seq": next_seq(change_dir),
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with _events_file(change_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_event_strict(
    change_dir: Path, event: AuditEvent | Mapping[str, object],
) -> None:
    try:
        validated = _AUDIT_ADAPTER.validate_python(event)
        _append(change_dir, validated.model_dump(mode="json", by_alias=True, exclude_none=True))
    except (EventWriteError, OSError, TypeError, ValueError, ValidationError) as exc:
        raise EventWriteError(str(exc)) from exc


def append_event_best_effort(change_dir: Path, event: Mapping[str, object]) -> None:
    try:
        _append(change_dir, event)
    except Exception as exc:  # telemetry 永不改变调用者结果；不捕获 BaseException
        print(f"warning: events.jsonl skipped: {exc}", file=sys.stderr)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: PASS（13 passed：6 基础行为 + 7 个 frozen audit shapes）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/core/events.py tests/unit/test_events.py
git commit -m "feat: add dual-mode events.jsonl writer with frozen event-shape contract"
```

---

### Task 6B: progression 文件快照原语（`workflow/core/snapshot.py`）

**Files:**
- Create: `assurance_agent/workflow/core/snapshot.py`
- Test: `tests/unit/test_snapshot.py`

**Interfaces:**
- Produces（M6 progression 必须直接消费）：
  - frozen dataclass `FileSnapshot(path: Path, existed: bool, content: bytes | None)`。
  - `capture_files(paths: Sequence[Path]) -> tuple[FileSnapshot, ...]`。
  - `restore_files(snapshots: Sequence[FileSnapshot]) -> None`——原文件用同目录临时文件 + `os.replace` 恢复；快照时不存在的文件在 rollback 时删除。
- 本模块不知道 event/state 语义；M6 负责「capture → strict event → state → on error restore」，不得复制 snapshot 实现。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_snapshot.py
from pathlib import Path

from assurance_agent.workflow.core.snapshot import capture_files, restore_files


def test_restore_reinstates_existing_and_removes_new_file(tmp_path: Path):
    state = tmp_path / "workflow-state.yaml"
    events = tmp_path / "events.jsonl"
    state.write_bytes(b"before-state")
    snapshots = capture_files([state, events])

    state.write_bytes(b"after-state")
    events.write_bytes(b"new-event\n")
    restore_files(snapshots)

    assert state.read_bytes() == b"before-state"
    assert not events.exists()


def test_capture_is_immutable_bytes_snapshot(tmp_path: Path):
    target = tmp_path / "x"
    target.write_bytes(b"v1")
    snapshots = capture_files([target])
    target.write_bytes(b"v2")
    assert snapshots[0].content == b"v1"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_snapshot.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 写最小实现**

```python
# assurance_agent/workflow/core/snapshot.py
"""Small file snapshots used by the M6 progression write boundary."""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    existed: bool
    content: bytes | None


def capture_files(paths: Sequence[Path]) -> tuple[FileSnapshot, ...]:
    return tuple(
        FileSnapshot(path=path, existed=path.exists(), content=path.read_bytes() if path.exists() else None)
        for path in paths
    )


def restore_files(snapshots: Sequence[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.existed:
            snapshot.path.unlink(missing_ok=True)
            continue
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot.path.with_suffix(snapshot.path.suffix + f".restore.{os.getpid()}")
        tmp.write_bytes(snapshot.content or b"")
        os.replace(tmp, snapshot.path)
```

- [ ] **Step 4: 运行测试与门禁**

Run: `uv run pytest tests/unit/test_snapshot.py -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 2 passed，全部门禁通过。

- [ ] **Step 5: Commit**

```bash
git add assurance_agent/workflow/core/snapshot.py tests/unit/test_snapshot.py
git commit -m "feat: freeze progression file snapshot boundary"
```

---

### Task 7: Gate 四态裁决（`workflow/orchestration/gates.py`）

**Files:**
- Create: `assurance_agent/workflow/orchestration/gates.py`
- Test: `tests/unit/test_gates.py`

**Interfaces:**
- Consumes: `schema.WorkflowSchema` / `GateDef` / `SchemaError`；`dsl.parse_expression` / `evaluate` / `Scope` / `MISSING`；`state.read_state`。
- Produces（供 M4/M6 消费）：
  - pydantic 模型 `GateVerdict`：`gate: str`、`verdict: schema.Verdict`、`matched_rule: str | None`、`reason: str | None`。schema 解析、内部 memo 和输出只使用这一份 `StrEnum`，不维护第二套 Literal。
  - `check_gate(schema, gate_name, change_dir, state, params) -> GateVerdict`。
  - `resolve_gate_verdict(schema, gate_name, change_dir, state, params) -> Verdict`——供 DSL `gate()` 内部调用（带 memo + 环检测）；`check_gate` 是它的对外 pydantic 封装。
  - `class GateCycleError(AaError)`。

裁决算法（对齐源版 `adjudicate`）：
1. `invalid_json: stop` 且任一 reads 文件存在但 JSON 解析失败 → `stop`。
2. 构造作用域：`reads[0]` 为 primary，其顶层字段 hoist 进根作用域；每个 read 的 alias → 其文档；根作用域还含 `params`、`state`（`state` 排除 `_integrity` 等下划线键）。
3. **规则按声明顺序求值，first-true-wins**（对齐源版 `adjudicate`：`for rule of gate.rules`）。安全语义（`needs_fix → needs_human_review → reject → pass`）由 **schema 加载期强制的 canonical 声明顺序**保证（Task 4 `_SAFETY_ORDER` 校验：这四个 verdict 若同时出现必须按此序声明），因此「声明顺序 == 安全优先级」，运行期无需再排序（避免与源版行为分叉、也避免对 `enter/skip/exit/continue` 等门做臆测排序）。求值中出现 `MISSING` 记账 `saw_missing`。
4. 无规则命中 + `saw_missing` + `missing_field_is` → 该 verdict。
5. 无规则命中 + 任一 reads 文件整体缺失 + `missing_file_is` → 该 verdict。
6. 兜底：`default`（缺省 `stop`，fail-closed）。

DSL 内建接线：`file_exists(p)` 支持 `repo:`（项目根相对）、`qa/`（项目根相对）、否则 change 相对；`gate(id)` 经 `resolve_gate_verdict` 递归裁决，同一次顶层裁决内 memo，环则抛 `GateCycleError`（被兜底为 `stop`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_gates.py
import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.orchestration.gates import check_gate, resolve_change_path
from assurance_agent.workflow.orchestration.schema import parse_schema

EMPTY = WorkflowState()  # gates 接收 WorkflowState，不接收裸 dict

SCHEMA = parse_schema("""
schema_version: "1"
name: t
params:
  force_continue: { type: bool, default: false }
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    needs_human_review_when: "decision == 'needs_human_review'"
    reject_when: "decision == 'reject'"
    pass_when: "decision == 'pass'"
""")


def _write_review(change_dir: Path, payload: dict) -> None:
    d = change_dir / "review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case-review.json").write_text(json.dumps(payload))


def test_pass(tmp_path: Path):
    _write_review(tmp_path, {"decision": "pass", "auto_fix_allowed": False})
    v = check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {})
    assert v.verdict == "pass"


def test_needs_fix_order_wins(tmp_path: Path):
    _write_review(tmp_path, {"decision": "needs_fix", "auto_fix_allowed": True})
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "needs_fix"


def test_reject(tmp_path: Path):
    _write_review(tmp_path, {"decision": "reject", "auto_fix_allowed": False})
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "reject"


def test_missing_field_is_stop(tmp_path: Path):
    _write_review(tmp_path, {"auto_fix_allowed": True})  # 无 decision
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_invalid_json_stop(tmp_path: Path):
    d = tmp_path / "review"
    d.mkdir(parents=True)
    (d / "case-review.json").write_text("{not json")
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_missing_file_default_stop(tmp_path: Path):
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_change_id_placeholder_resolves_for_archive_produce(tmp_path: Path):
    change = tmp_path / "qa" / "changes" / "C-42"
    assert resolve_change_path(change, "qa/archive/<change-id>/") == (
        tmp_path / "qa" / "archive" / "C-42"
    )


def test_safety_order_declaration_first_true_wins(tmp_path: Path):
    """加载期强制 canonical 安全序声明 → 声明顺序即安全优先级；needs_fix 先于 pass 命中。

    fixture 里 needs_fix_when 与 pass_when 同时为真，needs_fix 声明在前 → 裁决 needs_fix。
    注意 skill!=null 的 phase 必须带白名单 agent（Task 4 校验），故 `r` 声明 agent。
    """
    schema = parse_schema('''
schema_version: "1"
name: t
phases:
  - id: r
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: g
gates:
  g:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'pass'"
    reject_when: "decision == 'reject'"
    pass_when: "decision == 'pass'"
''')
    _write_review(tmp_path, {"decision": "pass"})   # satisfies BOTH needs_fix_when and pass_when
    assert check_gate(schema, "g", tmp_path, EMPTY, {}).verdict == "needs_fix"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_gates.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 写实现**

```python
# assurance_agent/workflow/orchestration/gates.py
"""Gate 四态裁决：读取 evidence，按规则顺序求值，输出单一 verdict。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.dsl import (
    MISSING,
    Scope,
    evaluate,
    parse_expression,
)
from assurance_agent.workflow.orchestration.schema import GateDef, ReadEntry, Verdict, WorkflowSchema


class GateVerdict(BaseModel):
    gate: str
    verdict: Verdict
    matched_rule: str | None = None
    reason: str | None = None


class GateCycleError(AaError):
    """gate() 递归裁决出现环。"""


def _project_root(change_dir: Path) -> Path:
    # change_dir = <root>/qa/changes/<id>
    return change_dir.parents[2] if len(change_dir.parents) >= 3 else change_dir


def resolve_change_path(change_dir: Path, rel: str) -> Path:
    """schema 路径 → 绝对路径：`repo:` / `qa/` 前缀相对项目根，否则相对 change 目录。

    对齐源版 `resolvePath`。供 gate reads 与引擎 produces-existence 共用。
    """
    root = _project_root(change_dir)
    change_id = change_dir.name
    normalized = rel.replace("<change-id>", change_id)
    if normalized.startswith("repo:"):
        return root / normalized[len("repo:"):]
    if normalized.startswith("qa/"):
        return root / normalized
    return change_dir / normalized


# 向后兼容别名（内部沿用）。
_resolve_path = resolve_change_path


def _load_doc(change_dir: Path, rel: str) -> tuple[bool, bool, object]:
    """returns (present, parse_error, value)."""
    path = _resolve_path(change_dir, rel)
    if not path.exists():
        return False, False, None
    try:
        text = path.read_text(encoding="utf-8")
        if rel.endswith((".yaml", ".yml")):
            return True, False, yaml.safe_load(text)
        return True, False, json.loads(text)
    except (json.JSONDecodeError, yaml.YAMLError, ValueError):
        return True, True, None


def _scope_state(state: WorkflowState) -> dict:
    # exclude_none 保留 DSL 的“未出现=MISSING”，同时保留 extra='allow' 扩展键。
    return state.model_dump(mode="json", exclude_none=True)


def build_evidence_scope(
    schema: WorkflowSchema,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    reads: list[ReadEntry] | None,
    *,
    hoist_primary: bool = True,
    memo: dict[str, Verdict] | None = None,
    stack: tuple[str, ...] = (),
) -> Scope:
    """构造 gate 规则与 phase when/ready_when **共用**的求值作用域（对齐源版 makeScope）。

    - **gate**（`hoist_primary=True`）：`reads[0]` primary 文档 hoist 顶层字段 + 各 read alias
      文档 + `params` + `state` + 内建 `file_exists()`/`gate()`。
    - **phase when/ready_when / loop allocate_on**（`hoist_primary=False`）：引擎传入
      `schema.produces_alias_map()` 派生的**全局反向别名** ReadEntry 列表（对齐源版
      `reverseAlias` / `predicateScope`，**不 hoist**），因此 `any(fix_proposal.proposals, ...)`、
      `gate('healing-entry-gate').verdict == 'enter'` 等打包 schema 条件在编排期即可求值——
      **无需任何 per-phase `reads` 字段**。
    """
    reads = reads or []
    alias_docs: dict[str, object] = {}
    for r in reads:
        _, _, val = _load_doc(change_dir, r.path)
        alias_docs[r.alias] = val
    primary_val = alias_docs.get(reads[0].alias) if (reads and hoist_primary) else None
    hoisted = primary_val if isinstance(primary_val, dict) else {}
    scope_vars: dict[str, object] = {
        **hoisted,
        **alias_docs,
        "params": params,
        "state": _scope_state(state),
    }
    _memo = memo if memo is not None else {}

    def file_exists(rel: str) -> bool:
        return _resolve_path(change_dir, rel).exists()

    def gate_verdict(gid: str) -> str:
        try:
            return resolve_gate_verdict(schema, gid, change_dir, state, params, _memo, stack).value
        except GateCycleError:
            return Verdict.STOP.value

    return Scope(scope_vars, file_exists=file_exists, gate_verdict=gate_verdict)


def resolve_gate_verdict(
    schema: WorkflowSchema,
    gate_name: str,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    _memo: dict[str, Verdict] | None = None,
    _stack: tuple[str, ...] = (),
) -> Verdict:
    memo = _memo if _memo is not None else {}
    if gate_name in memo:
        return memo[gate_name]
    if gate_name in _stack:
        raise GateCycleError("gate reference cycle: " + " -> ".join([*_stack, gate_name]))
    gate = schema.gates.get(gate_name)
    if gate is None:
        return Verdict.STOP
    verdict = _adjudicate(schema, gate, change_dir, state, params, memo, (*_stack, gate_name))[0]
    memo[gate_name] = verdict
    return verdict


def _adjudicate(
    schema: WorkflowSchema,
    gate: GateDef,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    memo: dict[str, Verdict],
    stack: tuple[str, ...],
) -> tuple[Verdict, str | None]:
    # Step 1 — invalid_json: stop
    if gate.invalid_json == Verdict.STOP:
        for r in gate.reads:
            present, parse_error, _ = _load_doc(change_dir, r.path)
            if present and parse_error:
                return Verdict.STOP, "invalid_json"

    # Step 2 — build scope (gate: primary hoist + aliases; shared helper with engine)
    scope = build_evidence_scope(schema, change_dir, state, params, gate.reads, memo=memo, stack=stack)

    # Step 3 — rules in DECLARATION order, first-true-wins (对齐源版 adjudicate)。
    # 安全语义（needs_fix→needs_human_review→reject→pass）由 schema 加载期强制的
    # canonical 声明顺序保证（Task 4 `_SAFETY_ORDER` 校验），因此声明顺序 == 安全优先级，
    # 无需运行期再排序（避免与源版行为分叉）。
    saw_missing = False
    for rule in gate.rules:
        result = evaluate(parse_expression(rule.expr), scope)
        if result is True:
            return rule.verdict, f"{rule.field}: {rule.expr}"
        if result is MISSING:
            saw_missing = True

    # Step 4 — missing_field_is
    if saw_missing and gate.missing_field_is:
        return gate.missing_field_is, "missing_field"

    # Step 5 — missing_file_is
    any_missing = any(not _resolve_path(change_dir, r.path).exists() for r in gate.reads)
    if any_missing and gate.missing_file_is:
        return gate.missing_file_is, "missing_file"

    # Step 6 — fail-closed default
    return gate.default, None


def check_gate(
    schema: WorkflowSchema,
    gate_name: str,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
) -> GateVerdict:
    gate = schema.gates.get(gate_name)
    if gate is None:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason="unknown gate")
    memo: dict[str, Verdict] = {}
    try:
        verdict, matched = _adjudicate(schema, gate, change_dir, state, params, memo, (gate_name,))
    except GateCycleError as exc:
        return GateVerdict(gate=gate_name, verdict=Verdict.STOP, reason=str(exc))
    return GateVerdict(gate=gate_name, verdict=verdict, matched_rule=matched)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_gates.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/orchestration/gates.py tests/unit/test_gates.py
git commit -m "feat: add four-state gate adjudication (declaration-order first-true-wins) with gate() memoization"
```

---

### Task 8: 打包 schema 全表达式对拍测试（`tests/unit/test_dsl_schema_corpus.py`）

净室重写的验收标准（spec 第 3、11 节）：**打包 `workflow-schema.yaml` 中出现的每一条表达式都必须有单测覆盖，且每条都同时对拍「真值路径」与「missing/否定路径」**。

设计要点（回应「只逐条 parse、真正求值的只有少数手写模式」）：
- 表达式**不手抄**——`_collect(schema)` 从加载后的 schema 直接抽取「位置 id → 表达式串」（phase `when`/`ready_when`、loop `allocate_on`、每条 gate 规则 `*_when`）。避免 YAML 折叠/空白与手抄不一致。
- `CORPUS` 以**位置 id** 为键（不以表达式串为键），每个键给出 `(真值作用域, missing 作用域+期望)`。
- **覆盖闸**：`set(_collect(schema)) == set(CORPUS)`——schema 新增/删改任何表达式都会让本测试失败，强制 corpus 与 schema 一一对应。
- **对拍闸**：对每个位置，取 schema 里的**真实**表达式，在真值作用域下断言 `is True`，在 missing/否定作用域下断言 `is MISSING`（纯 `file_exists` 表达式无字段 MISSING，其否定路径断言 `is False`）。

**Files:**
- Test: `tests/unit/test_dsl_schema_corpus.py`

**Interfaces:**
- Consumes: `schema.load_workflow_schema`、`dsl.parse_expression`/`evaluate`/`Scope`/`MISSING`。

- [ ] **Step 1: 写测试（此处即最终测试，无独立实现步骤）**

```python
# tests/unit/test_dsl_schema_corpus.py
"""对拍：打包 schema 的每条表达式都有真值 + missing 覆盖（位置 id 驱动，与 schema 一一对应）。"""
from pathlib import Path

import pytest

from assurance_agent.workflow.orchestration.dsl import MISSING as MISS
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.schema import WorkflowSchema, load_workflow_schema


def _collect(schema: WorkflowSchema) -> dict[str, str]:
    """位置 id → 表达式串（从加载后的 schema 直接抽取，绝不手抄）。"""
    out: dict[str, str] = {}
    for p in schema.phases:
        if p.when:
            out[f"phase:{p.id}:when"] = p.when
        if p.ready_when:
            out[f"phase:{p.id}:ready_when"] = p.ready_when
    for lid, loop in schema.loops.items():
        if loop.allocate_on:
            out[f"loop:{lid}:allocate_on"] = loop.allocate_on
    for gid, g in schema.gates.items():
        for r in g.rules:
            out[f"gate:{gid}:{r.field}"] = r.expr
    return out


# ---- 作用域构造小工具 -------------------------------------------------------
def gv(v):
    return {"gate_verdict": lambda _i, _v=v: _v}


def fx(b):
    return {"file_exists": lambda _p, _b=b: _b}


def gvfx(v, b):
    return {"gate_verdict": lambda _i, _v=v: _v, "file_exists": lambda _p, _b=b: _b}


# run_mode/test_types/run_tests/auto_archive 全满足——覆盖所有「params.* when」的真值路径。
P_FULL = {"params": {
    "run_mode": "full",
    "test_types": ["api", "e2e", "fuzz", "performance"],
    "run_tests": True,
    "auto_archive": True,
    "max_healing_attempts": 3,
    "force_continue": False,
}}

# 每项：位置 id -> ((真值 vars, 真值 kw), (missing vars, missing kw, 期望))
CORPUS: dict[str, tuple[tuple[dict, dict], tuple[dict, dict, object]]] = {}

# --- 纯 params 的 phase when：P_FULL 全为真；空作用域全为 MISSING ---
for _pid in [
    "explore", "case-design", "fact-baseline", "api-plan", "api-plan-review",
    "api-codegen", "e2e-plan", "e2e-plan-review", "e2e-codegen", "fuzz-plan",
    "fuzz-plan-review", "fuzz-codegen", "performance-plan", "performance-plan-review",
    "performance-codegen", "execution", "archive",
]:
    CORPUS[f"phase:{_pid}:when"] = ((P_FULL, {}), ({}, {}, MISS))

# --- gate() 驱动的 phase when ---
CORPUS["phase:case-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:api-plan-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:e2e-plan-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:fix-proposal:when"] = ((P_FULL, gv("enter")), ({}, gv(MISS), MISS))

# --- any(...) 驱动的 phase when ---
CORPUS["phase:api-codegen-fix:when"] = (
    ({"fix_proposal": {"proposals": [{"target": "api", "eligible": True}]}}, {}), ({}, {}, MISS),
)
CORPUS["phase:e2e-codegen-fix:when"] = (
    ({"fix_proposal": {"proposals": [{"target": "e2e", "eligible": True}]}}, {}), ({}, {}, MISS),
)

# --- phase ready_when（report）---
CORPUS["phase:report:ready_when"] = (
    ({"state": {"phases": {"healing": {"status": "resolved"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)

# --- loop allocate_on（healing）---
CORPUS["loop:healing:allocate_on"] = (
    ({"fix_proposal": {"summary": {"eligible_count": 2}}}, fx(True)),
    ({}, fx(True), MISS),
)

# --- registry-gate ---
CORPUS["gate:registry-gate:pass_when"] = (
    ({"state": {"phases": {"skill_registry_check": {"status": "pass"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)
CORPUS["gate:registry-gate:stop_when"] = (
    ({"state": {"phases": {"skill_registry_check": {"status": "fail"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)

# --- case-review-gate / api|e2e-plan-review-gate（decision 型，四态）---
_NF = ({"decision": "needs_fix", "auto_fix_allowed": True}, {})
for _gid in ["case-review-gate", "api-plan-review-gate", "e2e-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (_NF, ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (({"decision": "needs_human_review"}, {}), ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:reject_when"] = (({"decision": "reject"}, {}), ({}, {}, MISS))
CORPUS["gate:case-review-gate:pass_when"] = (({"decision": "pass"}, {}), ({}, {}, MISS))
for _gid in ["api-plan-review-gate", "e2e-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({"decision": "pass", "codegen_readiness": "ready"}, {}), ({}, {}, MISS))

# --- fuzz|performance-plan-review-gate（decision in [...] 型）---
for _gid in ["fuzz-plan-review-gate", "performance-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (_NF, ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (({"decision": "changes_requested"}, {}), ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:reject_when"] = (({"decision": "reject"}, {}), ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:pass_when"] = (({"decision": "approved"}, {}), ({}, {}, MISS))

# --- case-design-gate ---
CORPUS["gate:case-design-gate:pass_when"] = (
    ({"state": {"run_context": {"interaction_mode": "autonomous"}}}, {}), ({}, {}, MISS),
)

# --- api|e2e-codegen-precondition-gate（gate() + file_exists）---
for _gid in ["api-codegen-precondition-gate", "e2e-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gvfx("pass", True)), ({}, gvfx(MISS, True), MISS))
    # stop_when = not file_exists(...)：真值 file_exists=False；否定路径 file_exists=True → False（无 MISSING 字段）
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, fx(False)), ({}, fx(True), False))

# --- fuzz|performance-codegen-precondition-gate（纯 gate()）---
for _gid in ["fuzz-codegen-precondition-gate", "performance-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gv("pass")), ({}, gv(MISS), MISS))
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, gv("stop")), ({}, gv(MISS), MISS))

# --- fixer-safety-gate ---
CORPUS["gate:fixer-safety-gate:pass_when"] = (
    ({"passed": True, "product_code_modified": False, "assertion_expected_value_changes_detected": False,
      "skip_or_xfail_added": False, "unrelated_tests_modified": False, "high_risk_proposal_applied": False}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:fixer-safety-gate:needs_human_review_when"] = (({"passed": False}, {}), ({}, {}, MISS))

# --- healing-entry-gate ---
CORPUS["gate:healing-entry-gate:enter_when"] = (
    ({"state": {"phases": {"execution": {"status": "FAIL"}, "inspect": {"inspect_mode": "primary"},
                           "healing": {"attempts_used": 0}}, "gates": {"healing_available": True}},
      "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
      "params": {"max_healing_attempts": 3}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:stop_when"] = (
    ({"state": {"phases": {"execution": {"status": "FAIL"}}, "gates": {"healing_available": False}},
      "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:skip_when"] = (
    ({"state": {"phases": {"execution": {"status": "PASS"}}}}, {}), ({}, {}, MISS),
)

# --- healing-loop-gate ---
CORPUS["gate:healing-loop-gate:exit_when"] = (
    ({"state": {"phases": {"execution": {"status": "PASS"}}}}, {}), ({"state": {"phases": {}}}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:continue_when"] = (
    ({"state": {"phases": {"execution": {"status": "FAIL"}, "healing": {"attempts_used": 0}}},
      "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
      "params": {"max_healing_attempts": 3}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:stop_when"] = (
    ({"state": {"phases": {"healing": {"attempts_used": 3}}}, "params": {"max_healing_attempts": 3}}, {}),
    ({"state": {"phases": {"healing": {}}}}, {}, MISS),
)

# --- archive-gate ---
CORPUS["gate:archive-gate:pass_when"] = (
    ({**P_FULL,
      "state": {"user_requested_archive": False,
                "phases": {"execution": {"status": "PASS", "batch_id": "b1"},
                           "healing": {"status": "resolved"}}},
      "case_review": {"decision": "pass"},
      "api_plan_review": {"decision": "pass"},
      "plan_review": {"decision": "pass"},
      "failure_analysis": {"source_batch_id": "b1", "failures": []}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:archive-gate:stop_when"] = (
    ({"state": {"phases": {"execution": {"status": "FAIL"}}}}, {}), ({}, {}, MISS),
)


def test_corpus_covers_every_packaged_expression(tmp_path: Path):
    locs = _collect(load_workflow_schema(tmp_path))  # 包内默认 schema
    assert set(locs) == set(CORPUS), (
        "CORPUS 必须与打包 schema 的表达式一一对应；"
        f"缺失={set(locs) - set(CORPUS)} 多余={set(CORPUS) - set(locs)}"
    )


@pytest.mark.parametrize("loc", sorted(CORPUS))
def test_truth_and_missing_pair(tmp_path: Path, loc: str):
    expr = _collect(load_workflow_schema(tmp_path))[loc]
    node = parse_expression(expr)
    (tv, tkw), (mv, mkw, mexp) = CORPUS[loc]
    assert evaluate(node, Scope(tv, **tkw)) is True, f"{loc} 真值路径应为 True：{expr}"
    assert evaluate(node, Scope(mv, **mkw)) is mexp, f"{loc} missing/否定路径不符：{expr}"
```

- [ ] **Step 2: 运行确认通过**

Run: `uv run pytest tests/unit/test_dsl_schema_corpus.py -v`
Expected: PASS（1 覆盖闸 + 66 个加载后位置 = 67 passed；YAML anchor 展开使 e2e gate 复用的 4 条规则也成为独立位置）。若打包 schema 增删表达式，覆盖闸立即失败，提示补 CORPUS。

- [ ] **Step 3: Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add tests/unit/test_dsl_schema_corpus.py
git commit -m "test: pin every packaged schema predicate with truth+missing pairs and coverage guard"
```

---

### Task 9A: produces-existence DAG 投影 + typed healing state（`engine.py` / `healing_state.py`）

**Files:**
- Create: `assurance_agent/workflow/orchestration/engine.py`
- Create: `assurance_agent/workflow/orchestration/healing_state.py`
- Test: `tests/unit/test_engine.py`
- Test: `tests/unit/test_healing_state.py`

**Interfaces:**
- Consumes: `schema.WorkflowSchema` / `PhaseDef` / `ReadEntry`；`gates.build_evidence_scope` / `resolve_gate_verdict` / `resolve_change_path`；`dsl.parse_expression` / `is_satisfied`；M2 `WorkflowState`。
- Produces（供 M4/M6 消费）：
  - pydantic 模型：
    - `DispatchEntry`：`phase_id: str`、`skill: str | None`、`agent: str | None`、`kind: Literal["skill","cli","orchestrator"]`。
    - `PhaseView`：`id: str`、`status: str`（`pruned|out_of_scope|blocked|ready|awaiting_gate|done|stopped`，对齐源版词汇）、`gate: str | None`、`gate_verdict: str | None`、`produces_present: bool`。
    - `Terminal`：`kind: Literal["completed","stopped","needs_human_review"]`、`reason: str | None`、`phase: str | None`。
  - `WorkflowStatus`：`phases: list[PhaseView]`、`next_dispatch: list[DispatchEntry]`、`terminal: Terminal | None`；Task 9B 增加带 inactive default factory 的 `healing_episode`，旧调用方可逐步迁移，但 `compute_status` 必须总是显式填入真实 projection。
  - typed `HealingStateSnapshot(status, attempts_used, all_fixers_no_op, episode_id, attempt_id)` 与 `HealingStateProvider` Protocol。
  - `derive_healing_state(change_dir) -> HealingStateSnapshot`：默认只读 typed ledger；找到最近未结束 episode，按 `operation_id` 去重其 `healing_attempt_allocated`。持久化 state 和 `heal_record_apply` 均不得改变 budget。
  - `compute_status(..., healing_provider=None) -> WorkflowStatus`——纯函数；provider 仅作为 M5 可替换派生实现，默认调用 `derive_healing_state`，二者都必须服从 allocation-event 计数合同。

引擎规则（**忠实转录源版 engine.ts**，clean-room 化——本轮 P0 重基线的核心）：

**进度模型（P0 重基线：produces 存在性，而非 state 状态串）**：phase 是否「已运行」由其 `produces` 文件**是否存在于磁盘**判定（对齐源版 `producesPresent`），**不再**读取 `state.phases.<id>.status` 作为进度。`state` 只作为 gate DSL 的证据（gate 表达式**按字面**读取 `state.phases.<key>.status`、`state.phases.healing.attempts_used` 等，引擎不解释状态词汇——这修复了「引擎只识别小写 done/pass/fail、与打包 schema 的 `PASS/PASS_WITH_WARNINGS` 及下划线 phase key 不兼容」的 P0）。

- **params 补缺省**：`params` 与 `schema.default_param_values()` 合并（显式 params 覆盖默认）。
- **healing overlay**：总是取 `provider or derive_healing_state` 的 typed snapshot，复制 state 后覆盖 `state.phases.healing`。绝不直接信任持久化 `attempts_used`，也不修改调用者传入对象。
- **单一 gate memo**：整个 `compute_status` 内共享一个 gate 裁决 memo（对齐源版单 Engine 实例），`when`/`ready_when`/exit-gate/嵌套 `gate()` 复用，避免重复求值与不一致。
- **predicate 作用域（P0 富作用域，全局反向别名）**：`when`/`ready_when` 在 `build_evidence_scope(..., reads=<全局反向别名>, hoist_primary=False)` 下求值——别名由 `schema.produces_alias_map()` 对**所有 phase 的 produces 全局反推**得到（`review/case-review.json → case_review`、`healing/fix-proposal.json → fix_proposal`……对齐源版 `deriveAlias`），因此 `gate('healing-entry-gate').verdict=='enter'`、`any(fix_proposal.proposals, ...)` 这类打包条件天然可用，**无需 phase.reads、也无需改打包 schema**（修复 P0：旧设计靠 phase.reads 装载 fix_proposal，默认 schema 未声明 → MISSING → 误跳过）。
- **剪枝（pruned）**：`when` 求值非 `True`（False 或 MISSING）→ `status=pruned`。若 phase 有 `requires` 但其**全部**前置均为 `pruned` → 该 phase 亦 `pruned`（传递剪枝）。
- **scope 过滤（修复 P0：`--scope execute` 此前不生效 / 跨 scope 死锁）**：`active_scope = None if scope=='full' else scope`。phase 在 active scope 内当且仅当 `phase.owned_by is None` 或 `active_scope in phase.owned_by`。**仅当 produces 不存在且不在 active scope 内**才记 `out_of_scope`（即：已在磁盘产出的 phase 无论 scope 都按其产出计 `done`，避免跨 scope 边 `fact-baseline → case-review` 因上游 out_of_scope 而死锁——`out_of_scope`/`pruned` 前置在 requires 里都视同「非阻塞」）。`scope` 形参由 M6 从 `--scope` 透传。
- **requires / isDone（含 repair 特例）**：`active_deps` 必须在依赖 view 已算出后过滤 `status not in {pruned,out_of_scope}`，不能只看预计算 `pruned` map。其余 `all/any_active/repair_of` 语义不变。新增真实 fixture：full-only upstream out_of_scope，execute-owned downstream 仍 ready。
- **repair 路由（P0 修复：repair/healing 此前进不去）**：若 `phase.repair_of` 指向的目标 phase 其 exit gate 当前裁决为 `needs_fix`（`repairRequested`）→ 该 repair phase 直接 `ready`（即便自身 produces 尚未刷新），从而把 healing-rerun 等重跑 phase 拉起。重跑写出新证据后，目标 phase 的 exit gate 复判 → `pass` 则记 `done` 放行下游。
- **gate 时机（P0 修复：绝不对未运行 phase 裁决 exit gate）**：exit gate **只在 `produces_present` 时裁决**；produces 未生成的 phase 直接走 `ready_when`/`ready`，其 gate 不被求值（空证据 fail-closed 不会误杀首个 `skill-registry-check`——满足 `test_initial_ready_is_reg_no_gate_adjudication`）。已运行 phase 的 gate 裁决路由：
  - `pass` → `done`；
  - `stop` / `reject`（`_TERMINAL_VERDICTS`）→ `stopped`（并汇聚为 `terminal=stopped`）；
  - `needs_human_review` → `awaiting_gate`（gate_verdict 记 `needs_human_review`）；无其它 `ready` 可推进时汇聚为 `terminal=needs_human_review`（exit 30，交人工/`aa decide`）；Task 9B 对**最新** `human_decision` 做最终投影：最新 action 为 `stop` 时 override 为 stopped，后续非 stop decision 可恢复计算。
  - `needs_fix` / 其它非终局 → `awaiting_gate`（gate_verdict 记实际值）：**不终止、不记 done**，因此下游 `isDone` 为假被阻塞，唯有其 repair phase 经上面的 repair 路由被拉起。
- **未运行 + ready_when**：`produces` 不存在且 requires 满足时，`ready_when` 非 `True` → `blocked`；否则 `ready`。无 gate 且 produces 已存在 → `done`。
- **healing 边界**：本 Task 只提供普通 phase view 与 typed state overlay；不得据“无 ready”猜 loop 结果。Task 9B 必须把 `loops.healing`、shared-output re-entry 和精确 STOP 汇入最终 `WorkflowStatus`，因此 Task 9A 不是可单独交付的 M3 终点。
- **next_dispatch**：所有 `ready` phase 按声明顺序输出 `DispatchEntry`；`kind`：phase.id ∈ `_INTERNAL_PHASES`（`skill-registry-check`/`test-infra-bootstrap`）→ `orchestrator`；`skill is None` → `cli`；否则 `skill`。terminal 非空时 `next_dispatch=[]`。
- **terminal 优先级**：任一 phase `stopped` → `stopped`；否则若无 `ready` 且存在 `awaiting_gate + needs_human_review` → `needs_human_review`；否则若无 `ready` 且全部「active（非 pruned/out_of_scope）」phase 均 `done` → `completed`；否则 `None`（仍在推进或等待 driver 分配/裁决）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_healing_state.py
from pathlib import Path

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def test_attempts_count_unique_allocations_in_current_episode(tmp_path: Path):
    _event(tmp_path, {"source": "heal", "type": "healing_entry_baseline_pinned",
                      "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
                      "entry_batch_id": "b1", "episode_id": "e1"})
    allocation = {"source": "progression", "type": "healing_attempt_allocated",
                  "episode_id": "e1", "attempt_id": "a1", "attempt_number": 1,
                  "operation_id": "op1", "source_batch_id": "b1"}
    _event(tmp_path, allocation)
    _event(tmp_path, allocation)  # replayed ledger line does not consume another attempt
    assert derive_healing_state(tmp_path).attempts_used == 1


def test_apply_without_allocation_does_not_consume_budget(tmp_path: Path):
    _event(tmp_path, {"source": "heal", "type": "heal_record_apply", "target": "api",
                      "proposal_sha256": "p", "source_batch_id": "b", "attempt_key": "p:b",
                      "summary_sha256": "s", "files_modified": []})
    assert derive_healing_state(tmp_path).attempts_used == 0
```

```python
# tests/unit/test_engine.py
import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.state import write_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import parse_schema

# 进度由 produces 文件存在性驱动；gate DSL 按字面读取 state.phases.<下划线 key>.status。
# 注意 phase id 用连字符（skill-registry-check），gate 读的 state key 用下划线（skill_registry_check）——
# 忠实复刻打包 schema 的词汇差异（本轮 P0）。
SCHEMA = parse_schema("""
schema_version: "1"
name: t
params:
  run_mode: { type: enum, values: [full, case-only], default: full }
phases:
  - id: skill-registry-check
    skill: null
    requires: []
    produces: [workflow-state.yaml]
    gate: reg-gate
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: [skill-registry-check]
    produces: [explore/advisory.json]
  - id: case
    skill: aa-case-design
    agent: aa-doc-author
    requires: [explore]
    produces: [.qa.yaml]
    when: "params.run_mode == 'full'"
gates:
  reg-gate:
    reads: [workflow-state.yaml]
    pass_when: "state.phases.skill_registry_check.status == 'pass'"
    stop_when: "state.phases.skill_registry_check.status == 'fail'"
""")

# needs_fix→healing 路由 + scope 归属；覆盖 produces-存在性 / repair 路由 / 耗尽三条 P0。
HEAL_SCHEMA = parse_schema("""
schema_version: "1"
name: h
params:
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: intake
    skill: aa-intake
    agent: aa-doc-author
    owned_by: [full]
    requires: []
    produces: [proposal.md]
  - id: execution
    skill: null
    owned_by: [full, execute]
    requires: [intake]
    produces: [execution/execution-manifest.yaml]
    gate: exec-gate
  - id: fix-proposal
    skill: aa-fixer
    agent: aa-test-author
    owned_by: [full, execute]
    requires: []
    when: "gate('healing-entry-gate').verdict == 'enter'"
    produces: [healing/fix-proposal.json]
gates:
  exec-gate:
    reads: [inspect/failure-analysis.json]
    needs_fix_when: "fix_proposal_eligible == true"
    pass_when: "fix_proposal_eligible == false"
    missing_file_is: pass
  healing-entry-gate:
    reads: [inspect/failure-analysis.json]
    enter_when: "fix_proposal_eligible == true and state.phases.healing.attempts_used < params.max_healing_attempts"
    skip_when: "fix_proposal_eligible == false"
    missing_file_is: skip
""")


def _pv(status, pid):
    return next(p for p in status.phases if p.id == pid)


def _write_fa(change_dir: Path, eligible: bool) -> None:
    d = change_dir / "inspect"
    d.mkdir(parents=True, exist_ok=True)
    (d / "failure-analysis.json").write_text(json.dumps({"fix_proposal_eligible": eligible}))


def _touch(change_dir: Path, rel: str) -> None:
    p = change_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")


def test_initial_ready_is_reg_no_gate_adjudication(tmp_path: Path):
    """P0: 无 produces → skill-registry-check 只是 ready（未运行），其 exit gate 不被裁决。"""
    st = compute_status(SCHEMA, tmp_path, WorkflowState(), {})
    assert _pv(st, "skill-registry-check").status == "ready"
    assert [d.phase_id for d in st.next_dispatch] == ["skill-registry-check"]
    assert st.next_dispatch[0].kind == "orchestrator"   # _INTERNAL_PHASES
    assert st.terminal is None


def test_when_prunes_case(tmp_path: Path):
    st = compute_status(SCHEMA, tmp_path, WorkflowState(), {"run_mode": "case-only"})
    assert _pv(st, "case").status == "pruned"


def test_downstream_ready_after_produces_and_gate_pass(tmp_path: Path):
    """produces 存在（workflow-state.yaml）+ gate pass → done；下游 explore → ready。"""
    # write_state 落盘 workflow-state.yaml（= skill-registry-check 的 produces），并写入 gate 读取的 status
    write_state(tmp_path, WorkflowState(phases={"skill_registry_check": {"status": "pass"}}))
    st = compute_status(SCHEMA, tmp_path, read_state_or(tmp_path), {})
    assert _pv(st, "skill-registry-check").status == "done"
    assert _pv(st, "explore").status == "ready"


def test_ran_phase_gate_stop_is_terminal(tmp_path: Path):
    write_state(tmp_path, WorkflowState(phases={"skill_registry_check": {"status": "fail"}}))
    st = compute_status(SCHEMA, tmp_path, read_state_or(tmp_path), {})
    assert st.terminal is not None and st.terminal.kind == "stopped"
    assert _pv(st, "skill-registry-check").status == "stopped"


def test_completed_when_all_produces_present_and_gates_pass(tmp_path: Path):
    write_state(tmp_path, WorkflowState(phases={"skill_registry_check": {"status": "pass"}}))
    _touch(tmp_path, "explore/advisory.json")   # explore produces（无 gate → done）
    _touch(tmp_path, ".qa.yaml")                # case produces（无 gate → done）
    st = compute_status(SCHEMA, tmp_path, read_state_or(tmp_path), {})
    assert st.terminal is not None and st.terminal.kind == "completed"


def test_scope_execute_marks_full_only_phase_out_of_scope(tmp_path: Path):
    """P0: --scope execute 生效 → 仅 full 的 intake（produces 未生成）out_of_scope，execution 仍可调度。"""
    st = compute_status(HEAL_SCHEMA, tmp_path, WorkflowState(), {}, scope="execute")
    assert _pv(st, "intake").status == "out_of_scope"
    assert _pv(st, "execution").status == "ready"
    assert "intake" not in [d.phase_id for d in st.next_dispatch]


def test_needs_fix_routes_to_healing_not_terminal(tmp_path: Path):
    """P0: execution produces 已生成 + gate=needs_fix → awaiting_gate（不终止）；fix-proposal 经 gate() when → ready。"""
    _write_fa(tmp_path, eligible=True)
    _touch(tmp_path, "execution/execution-manifest.yaml")   # execution 已运行（produces 存在）
    _touch(tmp_path, "proposal.md")                          # intake 已完成（无 gate → done）
    st = compute_status(HEAL_SCHEMA, tmp_path, WorkflowState(), {})
    assert st.terminal is None
    ev = _pv(st, "execution")
    assert ev.status == "awaiting_gate" and ev.gate_verdict == "needs_fix"
    assert _pv(st, "fix-proposal").status == "ready"
    assert [d.phase_id for d in st.next_dispatch] == ["fix-proposal"]


```

> 辅助：`read_state_or(change_dir)` = `read_state(change_dir) or WorkflowState()`（读回落盘的 state，供 gate DSL 读取 status）。测试顶部加：
> ```python
> from assurance_agent.workflow.core.state import read_state
> def read_state_or(cd: Path) -> WorkflowState:
>     return read_state(cd) or WorkflowState()
> ```
>
> 说明：普通 phase 进度由 produces 文件存在性驱动；healing attempt 计数不在这些测试里手写，默认 provider 从空 ledger 投影为 0。耗尽、复用产物与 continue 全部在 Task 9B/10 通过 typed ledger 测试。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_engine.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 写实现**

```python
# assurance_agent/workflow/orchestration/healing_state.py
"""Typed projection of the durable healing allocation ledger."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from assurance_agent.workflow.core.events import read_events

_TERMINAL = {"resolved", "not_needed", "skipped", "exhausted", "failed"}


class HealingStateSnapshot(BaseModel):
    status: str = "pending"
    attempts_used: int = Field(default=0, ge=0)
    all_fixers_no_op: bool = False
    episode_id: str | None = None
    attempt_id: str | None = None


class HealingStateProvider(Protocol):
    def __call__(self, change_dir: Path) -> HealingStateSnapshot: ...


def derive_healing_state(change_dir: Path) -> HealingStateSnapshot:
    events = read_events(change_dir)
    baseline = next(
        (e for e in reversed(events) if e.get("type") == "healing_entry_baseline_pinned"),
        None,
    )
    if baseline is None:
        return HealingStateSnapshot()
    baseline_seq = int(baseline.get("seq", 0))
    ended = any(
        int(e.get("seq", 0)) > baseline_seq
        and (
            (e.get("type") == "heal_transition" and e.get("to") in _TERMINAL)
            or (e.get("type") == "human_decision" and e.get("action") == "stop")
        )
        for e in events
    )
    if ended:
        return HealingStateSnapshot(status="not_needed")

    episode_id = str(baseline["episode_id"])
    allocations = [
        e for e in events
        if e.get("type") == "healing_attempt_allocated" and e.get("episode_id") == episode_id
    ]
    unique = {str(e["operation_id"]): e for e in allocations}
    latest = max(unique.values(), key=lambda e: int(e.get("seq", 0)), default=None)
    after_allocation = int(latest.get("seq", 0)) if latest else baseline_seq
    apply_events = [
        e for e in events
        if e.get("type") == "heal_record_apply" and int(e.get("seq", 0)) > after_allocation
    ]
    transitions = [
        e for e in events
        if e.get("type") == "heal_transition" and int(e.get("seq", 0)) > baseline_seq
    ]
    status = str(transitions[-1]["to"]) if transitions else "pending"
    return HealingStateSnapshot(
        status=status,
        attempts_used=len(unique),
        all_fixers_no_op=bool(apply_events) and all(not e.get("files_modified") for e in apply_events),
        episode_id=episode_id,
        attempt_id=str(latest["attempt_id"]) if latest else None,
    )
```

```python
# assurance_agent/workflow/orchestration/engine.py
"""DAG 状态引擎（忠实转录 engine.ts）：produces-存在性 + gate 裁决 驱动进度；纯函数。"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied, parse_expression
from assurance_agent.workflow.orchestration.gates import (
    build_evidence_scope,
    resolve_change_path,
    resolve_gate_verdict,
)
from assurance_agent.workflow.orchestration.healing_state import (
    HealingStateProvider,
    HealingStateSnapshot,
    derive_healing_state,
)
from assurance_agent.workflow.orchestration.schema import PhaseDef, ReadEntry, Verdict, WorkflowSchema

# 调度时视为「编排器内置」的 phase（对齐源版 resolveNextDispatch 的 internal 分支）。
# 注意：这与 schema.py 的 ORCHESTRATOR_INTERNAL（agent 豁免校验，仅 skill-registry-check）是
# 两个不同用途的集合，故此处独立命名、不复用 schema 的常量。
_INTERNAL_PHASES = {"skill-registry-check", "test-infra-bootstrap"}

_TERMINAL_VERDICTS = {Verdict.STOP, Verdict.REJECT}


class DispatchEntry(BaseModel):
    phase_id: str
    skill: str | None = None
    agent: str | None = None
    kind: Literal["skill", "cli", "orchestrator"]


class PhaseView(BaseModel):
    id: str
    # pruned | out_of_scope | blocked | ready | awaiting_gate | done | stopped（对齐源版词汇）
    status: str
    gate: str | None = None
    gate_verdict: str | None = None
    produces_present: bool = False


class Terminal(BaseModel):
    kind: Literal["completed", "stopped", "needs_human_review"]
    reason: str | None = None
    phase: str | None = None


class WorkflowStatus(BaseModel):
    phases: list[PhaseView]
    next_dispatch: list[DispatchEntry]
    terminal: Terminal | None = None


def _dispatch_kind(phase: PhaseDef) -> Literal["skill", "cli", "orchestrator"]:
    if phase.id in _INTERNAL_PHASES:
        return "orchestrator"
    return "cli" if phase.skill is None else "skill"


def _in_active_scope(phase: PhaseDef, active_scope: str | None) -> bool:
    # active_scope=None（full）→ 全部在内；否则按 owned_by 归属。
    if active_scope is None or phase.owned_by is None:
        return True
    return active_scope in phase.owned_by


def _file_exists(change_dir: Path, rel: str) -> bool:
    p = resolve_change_path(change_dir, rel)
    if rel.endswith("/"):  # 目录 produces：需存在且非空（对齐源版 fileExists 目录分支）
        return p.is_dir() and any(p.iterdir())
    return p.exists()


def _produces_present(change_dir: Path, phase: PhaseDef) -> bool:
    return bool(phase.produces) and all(_file_exists(change_dir, r) for r in phase.produces)


def _topo_order(schema: WorkflowSchema) -> list[str]:
    """Kahn 拓扑排序；同层按声明顺序稳定输出。schema 加载期已校验无环。"""
    ids = [p.id for p in schema.phases]
    indeg = {i: 0 for i in ids}
    deps: dict[str, list[str]] = {p.id: [d for d in p.requires if d in indeg] for p in schema.phases}
    for i in ids:
        indeg[i] = len(deps[i])
    order: list[str] = []
    ready = [i for i in ids if indeg[i] == 0]  # 保持声明顺序
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for p in schema.phases:  # 声明顺序扫描，稳定
            if cur in deps[p.id]:
                indeg[p.id] -= 1
                if indeg[p.id] == 0:
                    ready.append(p.id)
    if len(order) != len(ids):
        raise AssertionError("phase DAG cycle escaped schema validation")
    return order


def _overlay_healing(
    state: WorkflowState, change_dir: Path, provider: HealingStateProvider | None,
) -> tuple[WorkflowState, HealingStateSnapshot]:
    """Overlay event-derived state on a copy; persisted attempts never win."""
    derived = (provider or derive_healing_state)(change_dir)
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases["healing"] = {
        **dict(phases.get("healing") or {}),
        **derived.model_dump(mode="python", exclude_none=True),
    }
    data["phases"] = phases
    return WorkflowState.model_validate(data), derived


def compute_status(
    schema: WorkflowSchema,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    *,
    scope: str = "full",
    healing_provider: HealingStateProvider | None = None,
) -> WorkflowStatus:
    merged_params = {**schema.default_param_values(), **params}
    state, derived_healing = _overlay_healing(state, change_dir, healing_provider)
    active_scope = None if scope == "full" else scope
    memo: dict[str, Verdict] = {}  # 单次 compute_status 内共享 gate 裁决 memo

    # when/ready_when 的 predicate 作用域：全局反向别名（over 所有 produces），不 hoist primary。
    alias_reads = [ReadEntry(path=path, alias=alias) for alias, path in schema.produces_alias_map().items()]

    def predicate_scope():
        return build_evidence_scope(
            schema, change_dir, state, merged_params, alias_reads,
            hoist_primary=False, memo=memo,
        )

    def gate_verdict(gate_id: str) -> Verdict:
        return resolve_gate_verdict(schema, gate_id, change_dir, state, merged_params, memo, ())

    by_id = {p.id: p for p in schema.phases}

    # 1) 剪枝：when 非 True（含传递剪枝）
    pruned: dict[str, bool] = {}
    for phase in schema.phases:
        pruned[phase.id] = bool(phase.when) and not is_satisfied(
            parse_expression(phase.when), predicate_scope()
        )

    views: dict[str, PhaseView] = {}
    for pid in _topo_order(schema):
        phase = by_id[pid]
        views[pid] = _phase_view(
            by_id, change_dir, phase, pruned, views, active_scope, predicate_scope, gate_verdict,
        )

    phases = [views[p.id] for p in schema.phases]  # 声明顺序输出
    ready = [
        DispatchEntry(phase_id=p.id, skill=by_id[p.id].skill, agent=by_id[p.id].agent, kind=_dispatch_kind(by_id[p.id]))
        for p in phases if p.status == "ready"
    ]
    terminal = _terminal(phases, ready)
    next_dispatch = [] if terminal is not None else ready
    return WorkflowStatus(phases=phases, next_dispatch=next_dispatch, terminal=terminal)


def _phase_view(
    by_id: dict[str, PhaseDef],
    change_dir: Path,
    phase: PhaseDef,
    pruned: dict[str, bool],
    views: dict[str, PhaseView],
    active_scope: str | None,
    predicate_scope: Callable[[], Scope],
    gate_verdict: Callable[[str], Verdict],
) -> PhaseView:
    gate = phase.gate

    if pruned[phase.id]:
        return PhaseView(id=phase.id, status="pruned", gate=gate)

    active_deps = [
        d for d in phase.requires
        if views[d].status not in ("pruned", "out_of_scope")
    ]
    if phase.requires and all(views[d].status == "pruned" for d in phase.requires):
        return PhaseView(id=phase.id, status="pruned", gate=gate)

    produces_present = _produces_present(change_dir, phase)

    if not produces_present and not _in_active_scope(phase, active_scope):
        return PhaseView(id=phase.id, status="out_of_scope", gate=gate)

    def is_done(dep_id: str) -> bool:
        if views[dep_id].status == "done":
            return True
        # repair 特例：本 phase 修复 dep，且 dep 的 exit gate 当前判 needs_fix → 视 dep「已就绪待修」
        if phase.repair_of != dep_id:
            return False
        dep = by_id.get(dep_id)
        return bool(dep and dep.gate) and gate_verdict(dep.gate) == "needs_fix"

    done_deps = [d for d in active_deps if is_done(d)]
    if phase.requires_mode == "any_active":
        blocked = bool(active_deps) and not done_deps
    else:
        blocked = any(not is_done(d) for d in active_deps)
    if blocked:
        return PhaseView(id=phase.id, status="blocked", gate=gate, produces_present=produces_present)

    # repair 路由：目标 phase 判 needs_fix → 拉起本 repair phase
    if phase.repair_of:
        target = by_id.get(phase.repair_of)
        if target and target.gate and gate_verdict(target.gate) == "needs_fix":
            return PhaseView(id=phase.id, status="ready", gate=gate, produces_present=produces_present)

    if not produces_present:
        if phase.ready_when and not is_satisfied(parse_expression(phase.ready_when), predicate_scope()):
            return PhaseView(id=phase.id, status="blocked", gate=gate)
        return PhaseView(id=phase.id, status="ready", gate=gate, produces_present=False)

    # produces 已生成 → 已运行：无 gate 即 done；有 gate 按裁决路由（gate 只在此处裁决）
    if not gate:
        return PhaseView(id=phase.id, status="done", produces_present=True)
    verdict = gate_verdict(gate)
    if verdict in _TERMINAL_VERDICTS:
        status = "stopped"
    elif verdict == "pass":
        status = "done"
    else:  # needs_fix / needs_human_review / 其它非终局 → 挂起
        status = "awaiting_gate"
    return PhaseView(id=phase.id, status=status, gate=gate, gate_verdict=verdict, produces_present=True)


def _terminal(phases: list[PhaseView], ready: list[DispatchEntry]) -> Terminal | None:
    stopped = next((p for p in phases if p.status == "stopped"), None)
    if stopped is not None:
        return Terminal(
            kind="stopped", phase=stopped.id,
            reason=f"gate '{stopped.gate}' verdict '{stopped.gate_verdict}'",
        )
    if ready:
        return None  # 还有可推进项，优先推进（needs_human_review 分支不阻断并行 ready）
    pending = next(
        (p for p in phases if p.status == "awaiting_gate" and p.gate_verdict == "needs_human_review"),
        None,
    )
    if pending is not None:
        return Terminal(
            kind="needs_human_review", phase=pending.id,
            reason=f"gate '{pending.gate}' needs human review",
        )
    active = [p for p in phases if p.status not in ("pruned", "out_of_scope")]
    if active and all(p.status == "done" for p in active):
        return Terminal(kind="completed")
    return None  # 普通 DAG 尚无结论；Task 9B 的 episode projection 再做 loop terminal 汇聚
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_engine.py tests/unit/test_healing_state.py -v`
Expected: PASS（9 passed：engine 7 + healing_state 2）

- [ ] **Step 5: 门禁 + Commit**

```bash
uv run ruff check . && uv run pyright && uv run lint-imports
git add assurance_agent/workflow/orchestration/engine.py assurance_agent/workflow/orchestration/healing_state.py \
        tests/unit/test_engine.py tests/unit/test_healing_state.py
git commit -m "feat: add DAG status engine (produces-existence progression, scope filter, gate-timing, repair routing)"
```

---

### Task 9B: 纯 healing episode projection + engine 汇聚

**Files:**
- Create: `assurance_agent/workflow/orchestration/healing_episode.py`
- Modify: `assurance_agent/workflow/orchestration/engine.py`
- Test fixture: `tests/fixtures/healing-episode-schema.yaml`
- Test: `tests/unit/test_healing_episode.py`

**Interfaces:**
- Produces `HealingAttemptIntent`、`HealingEpisodeAction`、`HealingEpisodeSnapshot` 与
  `project_healing_episode(schema, change_dir, state, params, healing) -> HealingEpisodeSnapshot`。
- `WorkflowStatus` 新增 `healing_episode`（inactive default factory 仅用于兼容手工构造）；`compute_status` 始终显式填真实 projection。episode 的 `dispatch_phase` 动作覆盖 loop member 的普通 produces status，因而 `healing-rerun` / `healing-reinspect` 即使旧产物存在也能重新变 `ready`。
- `allocate_attempt` 是写意图而非 `DispatchEntry`；其 `allocation` 携带完整的 `episode_id` / `attempt_id` / `attempt_number` / `operation_id` / `source_batch_id` / `pin_entry_baseline`。M6 只需按 intent 使用 Task 6B snapshot，并严格追加 baseline（仅首轮）与 `HealingAttemptAllocatedEvent`，不得自行推导业务字段。`operation_id` 由 `source_batch_id + proposal_sha256 + next_attempt_number` 确定性生成，重算不变。

**决策表（全部由 unit/golden 锁死）：**

| 当前 ledger/evidence | stage | 唯一 next action |
|---|---|---|
| entry=`enter`，本轮无 fix-proposal outcome | proposal | dispatch `fix-proposal` |
| proposal 已 commit 且 `allocate_on=True` | allocate | `allocate_attempt(operation_id)` |
| allocation 后仍有 eligible target 未 `heal_record_apply` | apply | dispatch 对应 fixer phase |
| apply 完成且 safety=`needs_human_review` | safety | await human |
| apply 完成且本 allocation 后无 rerun outcome | rerun | dispatch `healing-rerun`（忽略旧 manifest） |
| rerun outcome 后无 reinspect outcome | reinspect | dispatch `healing-reinspect`（忽略旧 inspect JSON） |
| reinspect 后 exit gate=`continue` | proposal | dispatch 下一轮 `fix-proposal` |
| exit gate=`exit` | terminal | complete `resolved` |
| exit gate=`stop`（budget/no-op） | terminal | `Terminal(stopped)`，reason 含 attempts/max |

- [ ] **Step 1: 写失败测试**

先写一份完整、可独立加载的 fixture；它刻意让 rerun/reinspect 覆盖原 execution/inspect 的 canonical 路径：

```yaml
# tests/fixtures/healing-episode-schema.yaml
schema_version: "1"
name: healing-episode-test
params:
  max_healing_attempts: { type: int, default: 2 }
phases:
  - id: execution
    skill: null
    requires: []
    produces: [execution/execution-manifest.yaml]
  - id: inspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [execution]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
  - id: fix-proposal
    skill: aa-fix-proposal
    agent: aa-doc-author
    requires: [inspect]
    produces: [healing/fix-proposal.json]
    loop: healing
  - id: api-codegen-fix
    skill: aa-api-codegen-fixer
    agent: aa-test-author
    requires: [fix-proposal]
    produces: [healing/api-apply-summary.json]
    loop: healing
  - id: healing-rerun
    skill: null
    requires: [api-codegen-fix]
    produces: [execution/execution-manifest.yaml]
    gate: fixer-safety-gate
    loop: healing
  - id: healing-reinspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [healing-rerun]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
    loop: healing
gates:
  healing-entry-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    enter_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    skip_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    missing_file_is: stop
    needs_human_review_when: "needs_review == true"
    pass_when: "passed == true"
  healing-loop-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    exit_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
    continue_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    stop_when: "state.phases.healing.attempts_used >= params.max_healing_attempts"
loops:
  healing:
    members: [fix-proposal, api-codegen-fix, healing-rerun, healing-reinspect]
    counter: state.phases.healing.attempts_used
    max_param: max_healing_attempts
    allocate_on: "file_exists('healing/fix-proposal.json') and fix_proposal.summary.eligible_count > 0"
    exit_gate: healing-loop-gate
```

```python
# tests/unit/test_healing_episode.py
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.healing_episode import (
    HealingEpisodeAction,
    project_healing_episode,
)
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state
from assurance_agent.workflow.orchestration.schema import parse_schema

FIXTURE = Path(__file__).parents[1] / "fixtures/healing-episode-schema.yaml"
SCHEMA = parse_schema(FIXTURE.read_text())


def _j(change: Path, rel: str, payload: object) -> None:
    path = change / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def _outcome(change: Path, phase: str, attempt: str) -> None:
    _event(change, {"source": "progression", "type": "phase_outcome_committed",
                    "phase": phase, "attempt_id": attempt, "gate_report": None})


def _allocate(change: Path, number: int) -> None:
    if number == 1:
        _event(change, {"source": "heal", "type": "healing_entry_baseline_pinned",
                        "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
                        "entry_batch_id": "b1", "episode_id": "e1"})
    _event(change, {"source": "progression", "type": "healing_attempt_allocated",
                    "episode_id": "e1", "attempt_id": f"ha{number}", "attempt_number": number,
                    "operation_id": f"op{number}", "source_batch_id": "b1"})


def test_allocate_action_requires_complete_typed_intent():
    with pytest.raises(ValidationError):
        HealingEpisodeAction(kind="allocate_attempt")


def test_shared_old_outputs_do_not_skip_rerun_or_reinspect(tmp_path: Path):
    _j(tmp_path, "execution/execution-manifest.yaml", {})       # old execution output
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(tmp_path, "inspect/quality-gate-result.json", {})         # old inspect output
    _j(tmp_path, "healing/fix-proposal.json", {"summary": {"eligible_count": 1},
        "proposals": [{"target": "api", "eligible": True}]})
    _j(tmp_path, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    _allocate(tmp_path, 1)
    _event(tmp_path, {"source": "heal", "type": "heal_record_apply", "target": "api",
                      "proposal_sha256": "p", "source_batch_id": "b1", "attempt_key": "p:b1",
                      "summary_sha256": "s", "files_modified": ["tests/x.py"]})
    state = WorkflowState(phases={"execution": {"status": "FAIL", "batch_id": "b1"},
                                  "inspect": {"inspect_mode": "primary"}},
                          gates={"healing_available": True})
    healing = derive_healing_state(tmp_path)
    episode = project_healing_episode(SCHEMA, tmp_path, state, {}, healing)
    assert episode.next_actions[0].phase == "healing-rerun"

    _outcome(tmp_path, "healing-rerun", "r1")
    episode = project_healing_episode(SCHEMA, tmp_path, state, {}, healing)
    assert episode.next_actions[0].phase == "healing-reinspect"


def test_allocate_operation_id_is_stable_and_ignores_persisted_attempts(tmp_path: Path):
    _j(tmp_path, "execution/execution-manifest.yaml", {})
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(tmp_path, "healing/fix-proposal.json", {"summary": {"eligible_count": 1},
        "proposals": [{"target": "api", "eligible": True}]})
    _outcome(tmp_path, "fix-proposal", "p1")
    # 旧 state 中伪造的 attempts_used=99 不能覆盖 event-derived snapshot(0)。
    state = WorkflowState(phases={"execution": {"status": "FAIL", "batch_id": "b1"},
                                  "healing": {"attempts_used": 99}},
                          gates={"healing_available": True})
    healing = derive_healing_state(tmp_path)
    first = project_healing_episode(SCHEMA, tmp_path, state, {}, healing)
    second = project_healing_episode(SCHEMA, tmp_path, state, {}, healing)
    assert first.next_actions[0].kind == "allocate_attempt"
    assert first.next_actions[0].allocation is not None
    assert second.next_actions[0].allocation is not None
    assert first.next_actions[0].allocation.operation_id == second.next_actions[0].allocation.operation_id
    assert first.next_actions[0].allocation.pin_entry_baseline is True
    assert first.attempt_number == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_healing_episode.py -v`
Expected: FAIL（module 不存在）。

- [ ] **Step 3: 写 projection 模型与最小实现**

```python
# assurance_agent/workflow/orchestration/healing_episode.py
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.orchestration.dsl import is_satisfied, parse_expression
from assurance_agent.workflow.orchestration.gates import build_evidence_scope, check_gate
from assurance_agent.workflow.orchestration.healing_state import HealingStateSnapshot
from assurance_agent.workflow.orchestration.schema import ReadEntry, WorkflowSchema


class HealingAttemptIntent(BaseModel):
    episode_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str = Field(min_length=1)
    pin_entry_baseline: bool


class HealingEpisodeAction(BaseModel):
    kind: Literal["dispatch_phase", "allocate_attempt", "await_human", "complete"]
    phase: str | None = None
    allocation: HealingAttemptIntent | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        valid = (
            self.kind == "dispatch_phase"
            and self.phase is not None
            and self.allocation is None
            and self.outcome is None
        ) or (
            self.kind == "allocate_attempt"
            and self.phase is None
            and self.allocation is not None
            and self.outcome is None
        ) or (
            self.kind == "await_human"
            and self.phase is None
            and self.allocation is None
            and self.outcome is None
        ) or (
            self.kind == "complete"
            and self.phase is None
            and self.allocation is None
            and self.outcome is not None
        )
        if not valid:
            raise ValueError(f"invalid healing action shape for kind={self.kind}")
        return self


class HealingEpisodeSnapshot(BaseModel):
    state: Literal["inactive", "active", "awaiting_human", "terminal"]
    stage: Literal["entry", "proposal", "allocate", "apply", "safety", "rerun", "reinspect", "decide"] | None
    attempt_number: int = 0
    next_actions: list[HealingEpisodeAction] = Field(default_factory=list)
    terminal_kind: Literal["stopped"] | None = None
    reason: str | None = None


def _seq(event: dict[str, object]) -> int:
    return int(event.get("seq", 0))


def _latest(
    events: list[dict[str, object]], event_type: str, *, phase: str | None = None, after: int = 0,
):
    matches = [
        e for e in events
        if e.get("type") == event_type
        and (phase is None or e.get("phase") == phase)
        and _seq(e) > after
    ]
    return max(matches, key=_seq, default=None)


def _episode_floor(events: list[dict[str, object]], healing: HealingStateSnapshot) -> int:
    if healing.episode_id:
        baselines = [
            e for e in events
            if e.get("type") == "healing_entry_baseline_pinned"
            and e.get("episode_id") == healing.episode_id
        ]
        return _seq(max(baselines, key=_seq)) if baselines else 0
    terminal = [
        e for e in events
        if (e.get("type") == "heal_transition" and e.get("to") in {"resolved", "exhausted", "failed"})
        or (e.get("type") == "human_decision" and e.get("action") == "stop")
    ]
    return _seq(max(terminal, key=_seq)) if terminal else 0


def _dispatch(phase: str, stage: str, attempt: int) -> HealingEpisodeSnapshot:
    return HealingEpisodeSnapshot(
        state="active", stage=stage, attempt_number=attempt,
        next_actions=[HealingEpisodeAction(kind="dispatch_phase", phase=phase)],
    )


def _allocation_intent(
    change_dir: Path,
    state: WorkflowState,
    healing: HealingStateSnapshot,
    attempt_number: int,
) -> HealingAttemptIntent | None:
    proposal = (change_dir / "healing/fix-proposal.json").read_bytes()
    batch = state.phases.execution.batch_id if state.phases.execution else ""
    if not batch:
        return None
    proposal_sha = hashlib.sha256(proposal).hexdigest()
    episode_id = healing.episode_id or hashlib.sha256(
        f"{change_dir.name}:{batch}:{proposal_sha}".encode()
    ).hexdigest()
    operation_id = hashlib.sha256(
        f"{batch}:{proposal_sha}:{attempt_number}".encode()
    ).hexdigest()
    return HealingAttemptIntent(
        episode_id=episode_id,
        attempt_id=f"ha-{episode_id[:12]}-{attempt_number}",
        attempt_number=attempt_number,
        operation_id=operation_id,
        source_batch_id=batch,
        pin_entry_baseline=healing.episode_id is None,
    )


def _allocate_snapshot(
    change_dir: Path,
    state: WorkflowState,
    healing: HealingStateSnapshot,
    *,
    current_attempt: int,
    next_attempt: int,
) -> HealingEpisodeSnapshot:
    intent = _allocation_intent(change_dir, state, healing, next_attempt)
    if intent is None:
        return HealingEpisodeSnapshot(
            state="terminal", stage="allocate", attempt_number=current_attempt,
            terminal_kind="stopped", reason="missing execution source_batch_id",
        )
    return HealingEpisodeSnapshot(
        state="active", stage="allocate", attempt_number=current_attempt,
        next_actions=[HealingEpisodeAction(kind="allocate_attempt", allocation=intent)],
    )


def project_healing_episode(
    schema: WorkflowSchema,
    change_dir: Path,
    state: WorkflowState,
    params: dict,
    healing: HealingStateSnapshot,
) -> HealingEpisodeSnapshot:
    loop = schema.loops.get("healing")
    if loop is None:
        return HealingEpisodeSnapshot(state="inactive", stage=None)
    merged = {**schema.default_param_values(), **params}
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases["healing"] = {
        **dict(phases.get("healing") or {}),
        **healing.model_dump(mode="python", exclude_none=True),
    }
    data["phases"] = phases
    state = WorkflowState.model_validate(data)
    events = read_events(change_dir)
    episode_floor = _episode_floor(events, healing)
    allocations = [
        e for e in events
        if e.get("type") == "healing_attempt_allocated"
        and healing.episode_id is not None
        and e.get("episode_id") == healing.episode_id
    ]
    allocation = max(allocations, key=_seq, default=None)

    if allocation is None:
        entry = check_gate(schema, "healing-entry-gate", change_dir, state, merged).verdict.value
        if entry == "skip":
            return HealingEpisodeSnapshot(state="inactive", stage=None)
        if entry == "stop":
            return HealingEpisodeSnapshot(
                state="terminal", stage="entry", terminal_kind="stopped",
                reason="healing-entry-gate=stop",
            )
        proposal = _latest(
            events, "phase_outcome_committed", phase="fix-proposal", after=episode_floor
        )
        if proposal is None:
            return _dispatch("fix-proposal", "proposal", 0)
        reads = [ReadEntry(path=p, alias=a) for a, p in schema.produces_alias_map().items()]
        scope = build_evidence_scope(schema, change_dir, state, merged, reads, hoist_primary=False)
        if not is_satisfied(parse_expression(loop.allocate_on), scope):
            return HealingEpisodeSnapshot(
                state="terminal", stage="allocate", terminal_kind="stopped",
                reason="healing allocate_on false after committed proposal",
            )
        return _allocate_snapshot(
            change_dir, state, healing, current_attempt=0, next_attempt=1,
        )

    allocation_seq = _seq(allocation)
    attempt = healing.attempts_used
    proposal_doc = json.loads((change_dir / "healing/fix-proposal.json").read_text())
    targets = {
        item["target"] for item in proposal_doc.get("proposals", [])
        if item.get("eligible") is True and item.get("target") in {"api", "e2e"}
    }
    applied = {
        str(e.get("target")) for e in events
        if e.get("type") == "heal_record_apply"
        and _seq(e) > allocation_seq
        and e.get("source_batch_id") == allocation.get("source_batch_id")
    }
    missing = sorted(targets - applied)
    if missing:
        phase = "api-codegen-fix" if missing[0] == "api" else "e2e-codegen-fix"
        return _dispatch(phase, "apply", attempt)

    safety = check_gate(schema, "fixer-safety-gate", change_dir, state, merged).verdict.value
    if safety == "needs_human_review":
        return HealingEpisodeSnapshot(
            state="awaiting_human", stage="safety", attempt_number=attempt,
            next_actions=[HealingEpisodeAction(kind="await_human")],
        )
    if safety != "pass":
        return HealingEpisodeSnapshot(
            state="terminal", stage="safety", attempt_number=attempt,
            terminal_kind="stopped", reason=f"fixer-safety-gate={safety}",
        )

    rerun = _latest(
        events, "phase_outcome_committed", phase="healing-rerun", after=allocation_seq
    )
    if rerun is None:
        return _dispatch("healing-rerun", "rerun", attempt)
    reinspect = _latest(
        events, "phase_outcome_committed", phase="healing-reinspect", after=_seq(rerun)
    )
    if reinspect is None:
        return _dispatch("healing-reinspect", "reinspect", attempt)

    loop_verdict = check_gate(schema, loop.exit_gate, change_dir, state, merged).verdict.value
    if loop_verdict == "exit":
        return HealingEpisodeSnapshot(
            state="terminal", stage="decide", attempt_number=attempt,
            next_actions=[HealingEpisodeAction(kind="complete", outcome="resolved")],
        )
    if loop_verdict == "stop":
        maximum = int(merged[loop.max_param])
        return HealingEpisodeSnapshot(
            state="terminal", stage="decide", attempt_number=attempt,
            terminal_kind="stopped", reason=f"healing attempts exhausted: {attempt}/{maximum}",
        )
    proposal = _latest(
        events, "phase_outcome_committed", phase="fix-proposal", after=_seq(reinspect)
    )
    if proposal is None:
        return _dispatch("fix-proposal", "proposal", attempt)
    return _allocate_snapshot(
        change_dir, state, healing,
        current_attempt=attempt, next_attempt=attempt + 1,
    )
```

- [ ] **Step 4: 汇入 `compute_status`**

在 `WorkflowStatus` 增加 `healing_episode: HealingEpisodeSnapshot = Field(default_factory=lambda: HealingEpisodeSnapshot(state="inactive", stage=None))`。default 只为 M4/M6/M8 中直接构造 status 的测试兼容；`compute_status` 不得依赖它。`compute_status` 得到 effective state 与 `HealingStateSnapshot` 后调用 projection；移除普通 DAG 产生的 loop-member dispatch，再加入 episode 的 `dispatch_phase` 动作，并把对应 `PhaseView.status` 覆盖为 `ready`。episode 为 active/awaiting_human 时不得产生 `completed`；`terminal_kind="stopped"` 时构造 `Terminal(kind="stopped", reason=episode.reason)`。最后读取 typed ledger 中 seq 最大的 `human_decision`：仅当**最新** action 为 `stop` 时覆盖为 `Terminal(stopped, reason, checkpoint)`，并清空 dispatch；旧 stop 后有更新的非 stop decision 时恢复正常投影。

```python
from assurance_agent.workflow.orchestration.healing_episode import (
    HealingEpisodeSnapshot,
    project_healing_episode,
)
from assurance_agent.workflow.core.events import read_events

# WorkflowStatus 字段（类定义处）：
healing_episode: HealingEpisodeSnapshot = Field(
    default_factory=lambda: HealingEpisodeSnapshot(state="inactive", stage=None)
)

# 放在普通 PhaseView/ready 初算之后、最终 return 之前：
healing_loop = schema.loops.get("healing")
loop_members = set(healing_loop.members if healing_loop else [])
episode = project_healing_episode(schema, change_dir, state, merged_params, derived_healing)
ready = [d for d in ready if d.phase_id not in loop_members]
for action in episode.next_actions:
    if action.kind == "dispatch_phase" and action.phase:
        phase = by_id[action.phase]
        views[action.phase] = views[action.phase].model_copy(update={"status": "ready"})
        ready.append(DispatchEntry(
            phase_id=phase.id, skill=phase.skill, agent=phase.agent, kind=_dispatch_kind(phase),
        ))
latest_decision = max(
    (e for e in read_events(change_dir) if e.get("type") == "human_decision"),
    key=lambda e: int(e.get("seq", 0)),
    default=None,
)
if latest_decision is not None and latest_decision.get("action") == "stop":
    terminal = Terminal(
        kind="stopped",
        reason=str(latest_decision.get("reason") or "stopped by human decision"),
        phase=str(latest_decision.get("checkpoint") or "workflow"),
    )
elif episode.terminal_kind == "stopped":
    terminal = Terminal(kind="stopped", reason=episode.reason, phase="healing")
elif episode.state in {"active", "awaiting_human"}:
    terminal = None
return WorkflowStatus(
    phases=[views[p.id] for p in schema.phases],
    next_dispatch=[] if terminal else ready,
    terminal=terminal,
    healing_episode=episode,
)
```

- [ ] **Step 5: 运行测试与门禁**

Run: `uv run pytest tests/unit/test_engine.py tests/unit/test_healing_state.py tests/unit/test_healing_episode.py -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（12 passed：engine 7 + healing_state 2 + healing_episode 3）；共享旧产物测试必须在实现前失败、实现后通过。

- [ ] **Step 6: Commit**

```bash
git add assurance_agent/workflow/orchestration/engine.py \
        assurance_agent/workflow/orchestration/healing_episode.py \
        tests/fixtures/healing-episode-schema.yaml \
        tests/unit/test_healing_episode.py
git commit -m "feat: project healing episodes from durable allocation ledger"
```

---

### Task 10: healing loop golden 推演 + 分层约束 + 全量回归

**Files:**
- Modify: `.importlinter`（新增 orchestration/core 层级约束）
- Test: `tests/integration/__init__.py`（若不存在）、`tests/integration/test_healing_golden.py`

**Interfaces:**
- Consumes: 全部 M3 产物。
- Produces: 无新增运行时产物；固化 event-derived healing loop 的进入、共享产物重跑、下一轮与精确耗尽路径，并把分层契约钉死。

- [ ] **Step 1: 写 golden 测试（打包 schema 真实 gate 校验 + 合成 schema 全链路推演）**

golden 覆盖两套 healing 机制，且**把 proposal→allocation event→apply event→rerun outcome→reinspect outcome→exit / exhaustion 串成一条推演**：

1. **打包 schema 锚点**：用真实 `workflow-schema.yaml` 校验 `healing-entry-gate`/`healing-loop-gate` 的 enter/exit/stop 三态（证明真实 gate 语义正确；每条表达式的全量对拍已由 Task 8 corpus 覆盖）。
2. **合成 schema 全链路**（`GOLDEN`，忠实复刻两种机制：`repair_of` 修复环 + `loop` 治愈环），逐步 `compute_status`，每步推进 canonical produces 与严格 audit events；`state.phases.healing.attempts_used` 故意不由测试写入，断言 projection 的唯一动作与 terminal 演进。

> 合成 schema 的理由：打包 schema 四分支（api/e2e/fuzz/performance）扇出巨大，端到端确定性推演需上百行前置产物；合成 schema 在**同构**结构上做确定性全链路推演，真实 gate 语义由锚点测试 + Task 8 corpus 保证。

```python
# tests/integration/test_healing_golden.py
"""healing 两机制 golden：打包 gate 锚点 + event-derived 全链路推演。"""
import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.gates import check_gate
from assurance_agent.workflow.orchestration.healing_episode import HealingEpisodeAction
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state
from assurance_agent.workflow.orchestration.schema import load_workflow_schema, parse_schema


def _mk_change(tmp_path: Path) -> Path:
    change = tmp_path / "qa" / "changes" / "C1"
    (change / "inspect").mkdir(parents=True)
    return change


def _j(change: Path, rel: str, payload) -> None:
    p = change / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))


def _touch(change: Path, rel: str) -> None:
    p = change / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")


def _pv(st, pid):
    return next(p for p in st.phases if p.id == pid)


def _ready(st):
    return [d.phase_id for d in st.next_dispatch]


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def _outcome(change: Path, phase: str, attempt_id: str) -> None:
    _event(change, {"source": "progression", "type": "phase_outcome_committed",
                    "phase": phase, "attempt_id": attempt_id, "gate_report": None})


def _allocate(change: Path, number: int, episode_id: str = "e1") -> None:
    if number == 1:
        _event(change, {"source": "heal", "type": "healing_entry_baseline_pinned",
                        "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
                        "entry_batch_id": "b1", "episode_id": episode_id})
    _event(change, {"source": "progression", "type": "healing_attempt_allocated",
                    "episode_id": episode_id, "attempt_id": f"ha{number}",
                    "attempt_number": number, "operation_id": f"op{number}",
                    "source_batch_id": "b1"})


def _commit_allocation_intent(change: Path, action: HealingEpisodeAction) -> None:
    intent = action.allocation
    assert intent is not None
    if intent.pin_entry_baseline:
        _event(change, {"source": "heal", "type": "healing_entry_baseline_pinned",
                        "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
                        "entry_batch_id": intent.source_batch_id,
                        "episode_id": intent.episode_id})
    _event(change, {"source": "progression", "type": "healing_attempt_allocated",
                    "episode_id": intent.episode_id, "attempt_id": intent.attempt_id,
                    "attempt_number": intent.attempt_number,
                    "operation_id": intent.operation_id,
                    "source_batch_id": intent.source_batch_id})


def _apply(change: Path, number: int) -> None:
    _event(change, {"source": "heal", "type": "heal_record_apply", "target": "api",
                    "proposal_sha256": f"p{number}", "source_batch_id": "b1",
                    "attempt_key": f"p{number}:b1", "summary_sha256": f"s{number}",
                    "files_modified": ["tests/test_api.py"]})


def _state(change: Path, execution_status: str = "FAIL") -> WorkflowState:
    base = WorkflowState(
        phases={"execution": {"status": execution_status, "batch_id": "b1"},
                "inspect": {"inspect_mode": "primary"}},
        gates={"healing_available": True},
    )
    data = base.model_dump(mode="python", exclude_none=True)
    data["phases"]["healing"] = derive_healing_state(change).model_dump(
        mode="python", exclude_none=True
    )
    return WorkflowState.model_validate(data)


# ---------- 打包 schema 锚点：真实 healing gate 三态 ----------
def test_packaged_healing_entry_enters_on_eligible_failure(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    state = _state(change)
    assert check_gate(schema, "healing-entry-gate", change, state, {"max_healing_attempts": 3}).verdict == "enter"


def test_packaged_healing_loop_exits_on_pass(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": []})
    _allocate(change, 1)
    state = _state(change, execution_status="PASS")
    assert check_gate(schema, "healing-loop-gate", change, state, {"max_healing_attempts": 3}).verdict == "exit"


def test_packaged_healing_loop_stops_when_exhausted(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _allocate(change, 1)
    _allocate(change, 2)
    _allocate(change, 3)
    state = _state(change)
    assert check_gate(schema, "healing-loop-gate", change, state, {"max_healing_attempts": 3}).verdict == "stop"


def test_compute_status_dispatches_registry_on_fresh_change(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    st = compute_status(schema, change, WorkflowState(), {})
    assert "skill-registry-check" in _ready(st)


# ---------- 合成 schema：两机制全链路推演 ----------
GOLDEN = parse_schema("""
schema_version: "1"
name: golden
params:
  max_case_fix_attempts: { type: int, default: 2 }
  max_healing_attempts: { type: int, default: 2 }
phases:
  # repair_of 修复环
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
  - id: case-fix
    skill: aa-case-fixer
    agent: aa-doc-author
    requires: [case-review]
    produces: [review/case-review-apply-summary.md]
    when: "gate('case-review-gate').verdict == 'needs_fix'"
    repair_of: case-review
    max_attempts_param: max_case_fix_attempts
  # healing 治愈环（execution 无 gate；FAIL 由 failure-analysis + healing-entry-gate 表达）
  - id: execution
    skill: null
    requires: [case-review]
    produces: [execution/execution-manifest.yaml]
  - id: inspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [execution]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
  - id: fix-proposal
    skill: aa-fix-proposal
    agent: aa-doc-author
    requires: [inspect]
    produces: [healing/fix-proposal.json]
    when: "gate('healing-entry-gate').verdict == 'enter'"
    loop: healing
  - id: api-codegen-fix
    skill: aa-api-codegen-fixer
    agent: aa-test-author
    requires: [fix-proposal]
    produces: [healing/api-apply-summary.json]
    when: "any(fix_proposal.proposals, target == 'api' and eligible == true)"
    loop: healing
  - id: healing-rerun
    skill: null
    requires: [api-codegen-fix]
    produces: [execution/execution-manifest.yaml]
    gate: fixer-safety-gate
    loop: healing
  - id: healing-reinspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [healing-rerun]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
    loop: healing
gates:
  case-review-gate:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    pass_when: "decision == 'pass'"
  healing-entry-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    enter_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    skip_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    missing_file_is: stop
    needs_human_review_when: "needs_review == true"
    pass_when: "passed == true"
  healing-loop-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    exit_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
    continue_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    stop_when: "state.phases.healing.attempts_used >= params.max_healing_attempts"
loops:
  healing:
    members: [fix-proposal, api-codegen-fix, healing-rerun, healing-reinspect]
    counter: state.phases.healing.attempts_used
    max_param: max_healing_attempts
    allocate_on: "file_exists('healing/fix-proposal.json') and fix_proposal.summary.eligible_count > 0"
    exit_gate: healing-loop-gate
""")


def test_golden_full_progression_enter_rerun_exit(tmp_path: Path):
    change = _mk_change(tmp_path)

    # Step 1 — case-review 判 needs_fix → case-fix 经 repair 路由变 ready（下游 execution 被阻塞）
    _j(change, "review/case-review.json", {"decision": "needs_fix", "auto_fix_allowed": True})
    st = compute_status(GOLDEN, change, _state(change), {})
    assert _pv(st, "case-review").gate_verdict == "needs_fix"
    assert _pv(st, "case-fix").status == "ready"
    assert _ready(st) == ["case-fix"]
    assert _pv(st, "execution").status == "blocked"

    # Step 2 — driver apply 修复 + 复评 pass → case-review done、case-fix pruned、execution ready
    _touch(change, "review/case-review-apply-summary.md")
    _j(change, "review/case-review.json", {"decision": "pass"})
    st = compute_status(GOLDEN, change, _state(change), {})
    assert _pv(st, "case-review").status == "done"
    assert _pv(st, "case-fix").status == "pruned"
    assert _pv(st, "execution").status == "ready"

    # Step 3 — 已有 execution/inspect canonical 产物仍应进入 episode。
    _touch(change, "execution/execution-manifest.yaml")
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(change, "inspect/quality-gate-result.json", {"decision": "fail"})
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.stage == "proposal"
    assert _ready(st) == ["fix-proposal"]

    # Step 4 — proposal outcome 只产生 allocate 写意图；driver 再严格追加 baseline/allocation。
    _j(change, "healing/fix-proposal.json",
       {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]})
    _outcome(change, "fix-proposal", "p1")
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.next_actions[0].kind == "allocate_attempt"
    _commit_allocation_intent(change, st.healing_episode.next_actions[0])

    # Step 5 — apply audit 完成后，即使旧 execution manifest 已存在也必须重派 rerun。
    _apply(change, 1)
    _touch(change, "healing/api-apply-summary.json")
    _j(change, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.stage == "rerun"
    assert _ready(st) == ["healing-rerun"]

    # Step 6 — rerun outcome 后，旧 inspect JSON 也不能跳过 reinspect。
    _outcome(change, "healing-rerun", "r1")
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.stage == "reinspect"
    assert _ready(st) == ["healing-reinspect"]

    # Step 7 — reinspect commit 后覆盖 canonical failure-analysis，无 eligible → resolved。
    _j(change, "inspect/failure-analysis.json", {"failures": []})
    _j(change, "inspect/quality-gate-result.json", {"decision": "pass"})
    _outcome(change, "healing-reinspect", "i1")
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.next_actions[0].outcome == "resolved"
    assert st.terminal is not None and st.terminal.kind == "completed"


def test_golden_second_attempt_exhausts_exactly_from_events(tmp_path: Path):
    change = _mk_change(tmp_path)
    _j(change, "review/case-review.json", {"decision": "pass"})
    _touch(change, "execution/execution-manifest.yaml")
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(change, "inspect/quality-gate-result.json", {"decision": "fail"})
    _j(change, "healing/fix-proposal.json",
       {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]})
    _j(change, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    _touch(change, "healing/api-apply-summary.json")

    _outcome(change, "fix-proposal", "p1")
    _allocate(change, 1)
    _apply(change, 1)
    _outcome(change, "healing-rerun", "r1")
    _outcome(change, "healing-reinspect", "i1")
    st = compute_status(GOLDEN, change, _state(change), {})
    assert st.healing_episode.stage == "proposal"

    _outcome(change, "fix-proposal", "p2")
    pending = compute_status(GOLDEN, change, _state(change), {})
    action = pending.healing_episode.next_actions[0]
    assert action.allocation is not None
    repeated = compute_status(GOLDEN, change, _state(change), {}).healing_episode.next_actions[0]
    assert repeated.allocation is not None
    assert action.allocation.operation_id == repeated.allocation.operation_id
    assert action.allocation.pin_entry_baseline is False
    _commit_allocation_intent(change, action)
    _apply(change, 2)
    _outcome(change, "healing-rerun", "r2")
    _outcome(change, "healing-reinspect", "i2")

    stopped = compute_status(GOLDEN, change, _state(change), {})
    assert derive_healing_state(change).attempts_used == 2
    assert stopped.healing_episode.terminal_kind == "stopped"
    assert "2/2" in (stopped.healing_episode.reason or "")
    assert stopped.terminal is not None and stopped.terminal.kind == "stopped"


def test_golden_latest_human_stop_overrides_dispatch(tmp_path: Path):
    change = _mk_change(tmp_path)
    _event(change, {
        "source": "decide", "type": "human_decision", "checkpoint": "workflow",
        "action": "stop", "reason": "operator halt", "who": "reviewer",
    })

    status = compute_status(GOLDEN, change, _state(change), {})

    assert status.terminal is not None
    assert status.terminal.kind == "stopped"
    assert status.terminal.reason == "operator halt"
    assert status.terminal.phase == "workflow"
    assert status.next_dispatch == []


def test_golden_later_non_stop_decision_resumes_projection(tmp_path: Path):
    change = _mk_change(tmp_path)
    _event(change, {
        "source": "decide", "type": "human_decision", "checkpoint": "workflow",
        "action": "stop", "reason": "pause", "who": "reviewer",
    })
    _event(change, {
        "source": "decide", "type": "human_decision", "checkpoint": "workflow",
        "action": "fix_and_proceed", "reason": "resume", "who": "reviewer",
    })

    status = compute_status(GOLDEN, change, _state(change), {})

    assert status.terminal is None or status.terminal.kind != "stopped"
    assert status.next_dispatch
```

- [ ] **Step 2: 运行确认通过**

Run: `uv run pytest tests/integration/test_healing_golden.py -v`
Expected: PASS（8 passed：4 打包锚点 + 2 合成全链路 + 2 条 latest-human-decision 投影）

- [ ] **Step 3: 更新分层约束 `.importlinter`（遵循系列「Import 契约（冻结）」，只增不换）**

> **冻结原则**：`.importlinter` 由 M1 建立，此后每个里程碑**只在既有 contract 内新增本里程碑的模块 / 新增独立命名的 forbidden 契约**，**绝不整文件替换、绝不反转既有层序、绝不改名既有 contract**（修复 Standards P1：M2/M5/M6 反复整文件覆盖导致依赖方向翻转）。冻结后的层序见系列文档「Import 契约（冻结）」，其中 `artifacts` 位于 `workflow` **之下**（`workflow → artifacts` 允许——本里程碑 `workflow/core/state.py` 正是 import `artifacts.WorkflowState`；反向 `artifacts → workflow` 禁止）。

编辑现有 `.importlinter`（不新建同类 `layers` contract）：在既有 `[importlinter:contract:layers]` 的 `layers` 中，确认顺序为 `cli > commands > (eval/retro) > workflow > artifacts > config > resources`（`artifacts` 由 M2 插入；本里程碑不改动 layers 本体）。

新增两条**独立命名**的 forbidden 契约（若已存在则跳过）：

```ini
# workflow.core 不得反向依赖 orchestration（core 在 orchestration 之下）。
# 注意：core → artifacts 是【允许】的（state.py import WorkflowState），故此处不禁 artifacts。
[importlinter:contract:core-below-orchestration]
name = workflow.core must not import workflow.orchestration
type = forbidden
source_modules =
    assurance_agent.workflow.core
forbidden_modules =
    assurance_agent.workflow.orchestration

# artifacts 是共享产物契约层，位于 workflow 之下，禁止反向依赖 workflow。
[importlinter:contract:artifacts-below-workflow]
name = artifacts must not import workflow
type = forbidden
source_modules =
    assurance_agent.artifacts
forbidden_modules =
    assurance_agent.workflow
```

- [ ] **Step 4: 全量回归 + 门禁**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright && uv run lint-imports`
Expected: 全部通过（M1 既有测试 + 本里程碑新增：exit_codes 2 + dsl_parse 9 + dsl_eval 8 + schema 13 + state 6 + events 13 + snapshot 2 + gates 8 + corpus 67 + engine/healing_state 9 + healing_episode 3 + healing_golden 8 = 148 个新测试）

- [ ] **Step 5: Commit**

```bash
git add .importlinter tests/integration/test_healing_golden.py tests/integration/__init__.py
git commit -m "test: add healing loop golden paths and orchestration layering contracts"
```

---

## M3 验收清单

- DSL：`ast.parse` 仅解析、白名单自解释、绝无 `eval`；三值逻辑（`MISSING` 传播）、typed 相等、二参数 `any/all/count` 隐式 lambda、`len/defined/file_exists/gate` 内建，与源版 `evaluator.ts` 语义对齐。
- 打包 schema 中**每条**表达式都有对拍单测且含 missing 路径（Task 8 corpus）。
- `WorkflowSchema` 加载期完成全部静态校验（语法、arity、gate 引用、环检测、引用完整性、alias 唯一），解析顺序 explicit(排他)→`.aa/`→`schemas/`→包内默认。
- `WorkflowSchema` 实现 `phase_produces` / `has_phase` / `gate_for_phase`（满足 M2 `WorkflowSchemaLike` 与 M4 需求）。
- Gate 裁决按**声明顺序 first-true-wins**（对齐源版），安全语义由加载期强制的 canonical 声明顺序（needs_fix→human_review→reject→pass，Spec §80）保证；容错序 invalid_json→规则→missing_field_is→missing_file_is→default；alias hoisting、`gate()` memo+环检测正确；schema 与 gate 共用唯一闭集 `Verdict(StrEnum)`。
- **state 类型统一**：`read_state`/`write_state` 以 M2 canonical `WorkflowState`（extra='allow'）为交换类型，不返回裸 dict（消除 M3/M4 类型契约破裂）；原子写 + 完整性哈希；篡改被探测、遗留无哈希态容忍；`state_guard(change_dir)` 返回 state 文件 SHA256 供 dispatch guard/幂等（对齐源版，**无 WAL/`_txn`**）。
- events 双模式（对齐源版，**无 WAL**）：`append_event_best_effort` 吞异常（telemetry），`append_event_strict` 失败抛 `EventWriteError`（audit）；`read_events` 跳过损坏行；冻结事件形状（`dispatch_signed`/`phase_outcome_committed`/`healing_attempt_allocated` 等）承载幂等（`attempt_id`/`operation_id`/`state_guard`）语义；事务性写边界（snapshot 捕获/回滚 + 幂等标记）在 M6 progression 层。
- `compute_status(scope=..., healing_provider=...)` 纯函数（**produces-存在性 + gate 裁决驱动普通 DAG，不读 status 串作进度**）：**scope/owned_by 过滤生效**（`--scope execute` 不重调度 full-only phase、且已产出 phase 不因 scope 死锁）；when/ready_when 在**全局反向别名富作用域**（params/state/gate()/file_exists() + `produces_alias_map()` 别名，**无 phase.reads**）下求值；**exit gate 只对 produces 已生成的 phase 裁决，绝不对未运行 phase 裁决**；needs_fix → `awaiting_gate` **不终止**，其 `repair_of` phase 经 repair 路由变 `ready`。
- healing episode 是纯 projection：attempt budget 只由当前 episode 的 `healing_attempt_allocated`（按 `operation_id` 去重）派生；共享 canonical rerun/reinspect 产物必须有本 allocation 之后的 `phase_outcome_committed` 才算完成；allocation、continue、human review、resolved 与精确 STOP 都只有一个 next action。
- healing loop 进入、共享产物重跑、退出、下一轮与耗尽路径 golden 通过。
- `uv run pytest` / `ruff` / `pyright` / `lint-imports` 全绿；新文件无 `aws` 残留；`.importlinter` 遵循冻结层序（只增不换），`workflow → artifacts` 允许、反向禁止。

## 接口契约符合性说明

- 严格实现计划系列「接口契约（冻结）」M3 段：`load_workflow_schema` / `WorkflowSchema`（+`phase_produces`/`has_phase`/`gate_for_phase`/`produces_alias_map`）、`MISSING` / `parse_expression` / `evaluate` / `Scope`、`build_evidence_scope`、`resolve_change_path`、`compute_status(scope=..., healing_provider=...)` / `DispatchEntry` / `Terminal` / `PhaseView` / `WorkflowStatus`、`derive_healing_state` / `project_healing_episode` / `HealingAttemptIntent` / `HealingEpisodeSnapshot`、`check_gate` / `resolve_gate_verdict` / `GateVerdict`、`read_state`(→`WorkflowState`) / `write_state`(`WorkflowState`) / `state_guard`、`append_event_best_effort` / `append_event_strict` / `read_events`、`capture_files` / `restore_files`、`exit_codes.py` 常量与 `exit_code_for_gate_verdict` / `exit_code_for_terminal`。
- **状态类型契约（冻结）**：`WorkflowState` 由 M2 定义（extra='allow'），M3/M4/M6 全部以它传递 state；引擎构造 DSL 作用域用 `state.model_dump(exclude_none=True)`；持久化的 `state.phases.healing` 仅是展示/兼容输入，gate 与 projection 使用的 attempts/status 每次被 typed-event snapshot 覆盖。
- **两处澄清（非契约偏离）**：
  1. 契约中 `evaluate(expr, scope)` 不显式带 `ctx`——本实现把 `file_exists`/`gate_verdict` 解析器折进 `Scope`，签名与契约一致。
  2. `Terminal.kind` 仅 `{completed, stopped, needs_human_review}`（无 `exhausted`）。healing projection 在 exit gate=`stop` 时直接令纯 `compute_status` 返回 `Terminal(kind="stopped")`；M6 只负责执行写意图与映射退出码，`exit_code_for_terminal` 对 stopped 返 20。
- **供 M4/M6（cascade 待办，本轮 m3_only 不改 M4–M9）**：M4 `state apply/heal/decide` 必须以 `WorkflowState` 属性读写（`state.phases`/`setattr healing`），healing 写 `state.phases.healing`；M6 `--scope` 透传给 `compute_status(scope=...)`；needs_fix gate 不得被 M4 拒绝 apply、不得被 M6 当作 phase failure（改为按引擎 next_dispatch 路由 healing）。
