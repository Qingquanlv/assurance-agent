"""graph 共享错误类型字面量。

存放在 core 层，使严格 core 事件模型与 graph 模型都从下层导入同一份
``ErrorKind``，避免 core → graph 的向上依赖。
"""

from typing import Literal

ErrorKind = Literal[
    "conflict",
    "timeout",
    "transport",
    "rate_limit",
    "auth",
    "invalid_input",
    "invalid_output",
    "forbidden_write",
    "contract",
    "internal",
]
