"""canonical target handler：agent 桥、AA domain operation 与 builtin gate/join/interrupt。

handler 只持有 ``TaskWorkspace`` 内的路径，返回 ``TaskResult``；strict ledger、
write-set 持久化与 canonical Update 均归 scheduler/runtime。``graph:<id>``
subgraph handler 随 Task 12 落地。
"""
