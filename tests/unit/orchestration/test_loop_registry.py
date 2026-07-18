"""loop_registry：注册表即 engine 与 loop kind 之间的唯一接缝。"""

import pytest

import assurance_agent.workflow.orchestration.engine  # noqa: F401 — 触发 episode 模块注册
from assurance_agent.workflow.orchestration import loop_registry
from assurance_agent.workflow.orchestration.loop_registry import LoopRegistryError
from assurance_agent.workflow.orchestration.schema import LoopDef, load_workflow_schema


def test_builtin_kinds_registered_after_engine_import():
    assert loop_registry.registered_kinds() == frozenset({"healing", "review_fix"})


def test_project_unknown_kind_is_fail_closed():
    loop = LoopDef(id="x", kind="bogus")
    with pytest.raises(LoopRegistryError, match="bogus"):
        loop_registry.project(
            loop_registry.LoopContext(schema=None, loc=None, state=None, params={}),  # type: ignore[arg-type]
            loop,
        )


def test_packaged_schema_kinds_all_registered(tmp_path):
    """schema 校验的是词表（静态集合），registry 校验的是接线（投影器存在）——
    两者不得漂移：打包 schema 里每个 loop kind 都必须有已注册投影器。"""
    schema = load_workflow_schema(tmp_path)
    for loop in schema.loops.values():
        assert loop.kind in loop_registry.registered_kinds(), (
            f"loop '{loop.id}' kind '{loop.kind}' unregistered"
        )
