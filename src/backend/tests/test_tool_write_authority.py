"""写权限按工具点名授权，而不是「这一轮允许写」（#793 的前置）。

原来是一个 ``allow_writes`` 布尔。它在只有 ``remember`` 一个写工具时是安全的，之后
就不是：打开它等于授权「这一轮工具面里凡是能写的都能写」，而工具面是动态拼的——
``default_tool_names`` 会把注册表里新加的只读工具自动收进去，而写工具一旦被某个调用
方放进名单，那个早就存在的会话就在没人改它的情况下多出一项写能力。

这组用例钉的是「授权对象是这几个工具，而不是『写』这个类别」。本次没有开任何新的门：
每个现存调用点翻译过去之后授权范围只会更窄。
"""

import pytest

from app.core.llm.router import LLMRouter
from app.tools.context import ToolContext
from app.tools.registry import ToolWriteDenied, run_tool, tool


def _ctx(**kw) -> ToolContext:
    return ToolContext(project_id=None, llm=LLMRouter(), **kw)


@pytest.fixture
def two_write_tools():
    """注册两个写工具：授权一个，不该顺带把另一个也放进来。"""
    from app.tools import registry

    names = ("_t_write_a", "_t_write_b", "_t_read")
    for name in names:

        @tool(
            name,
            description="test-only tool",
            input_schema={"type": "object", "properties": {}},
            # 最后那个是只读的：用它来验证守卫没把检索面一起关掉，
            # 比借用真工具干净——真工具还要 user_id、要库，噪声都不是这里要测的
            read_only=name == "_t_read",
            scope="user",
        )
        async def _touch(_ctx, _args):  # noqa: B023 — 三个工具共用同一个 handler
            return {"ok": True}

    yield names
    for name in names:
        registry._REGISTRY.pop(name, None)


async def test_a_named_write_tool_runs(two_write_tools):
    a, _b, _r = two_write_tools
    assert await run_tool(_ctx(writable=frozenset({a})), a, {}) == {"ok": True}


async def test_authorising_one_write_tool_does_not_authorise_the_other(two_write_tools):
    """这就是布尔挡不住的那件事：授权 A，B 也跟着能跑。"""
    a, b, _r = two_write_tools
    ctx = _ctx(writable=frozenset({a}))
    with pytest.raises(ToolWriteDenied):
        await run_tool(ctx, b, {})


async def test_the_default_context_runs_no_write_tool(two_write_tools):
    """默认空集：忘了授权的后果是拒绝执行，而不是静默改数据。"""
    a, _b, _r = two_write_tools
    with pytest.raises(ToolWriteDenied):
        await run_tool(_ctx(), a, {})


async def test_read_only_tools_need_no_authority(two_write_tools):
    """只读工具不受影响——否则这层守卫会把整个检索面一起关掉。"""
    *_writes, read_tool = two_write_tools
    assert await run_tool(_ctx(), read_tool, {}) == {"ok": True}


async def test_naming_a_read_only_tool_is_harmless(two_write_tools):
    """名单里混进一个只读工具不该有任何效果（也不该报错）。"""
    *_writes, read_tool = two_write_tools
    assert await run_tool(_ctx(writable=frozenset({read_tool})), read_tool, {}) == {"ok": True}


# ---------------------------------------------------------------------------
# 调用点：现存的两个授权方各自只授权自己那几个
# ---------------------------------------------------------------------------


def test_the_chat_turn_names_only_the_memory_tools():
    """对话轮次授权的是记忆工具，不是「这一轮能写」。

    读源码而不是跑一轮对话：这里要钉的是授权的**形状**，而跑一轮会把它藏在
    一堆 LLM 交互后面。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "app" / "api" / "chat_agent.py"
    text = src.read_text()
    assert "writable=frozenset(MEMORY_TOOL_NAMES) if memory_on else frozenset()" in text
    assert "allow_writes=memory_on" not in text


def test_the_mcp_path_authorises_only_the_tool_being_called():
    """外部 MCP：这次请求要哪个工具就只授权哪个，profile 已经放行过它。"""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "app" / "mcp" / "dispatch.py"
    text = src.read_text()
    assert "writable=frozenset({name}) if may_write else frozenset()" in text


def test_tool_context_has_no_blanket_write_flag():
    """布尔不该以任何形式留下来——留着就会有人再用它。"""
    assert not hasattr(ToolContext(project_id=None, llm=LLMRouter()), "allow_writes")
    assert isinstance(ToolContext(project_id=None, llm=LLMRouter()).writable, frozenset)
