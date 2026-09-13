"""外部 MCP 服务器接入（对外调用方向）。

对着**真的** MCP 服务器跑（tests/fixtures/fake_mcp_server.py，官方 SDK 起的 stdio
服务），不 mock 协议——这条链路的全部价值就是与真实服务器互通，把协议 mock 掉
等于什么都没验。
"""

import sys
from pathlib import Path

import pytest

from app.services.mcp_hub.bridge import (
    NAMESPACE,
    drop_server_tools,
    qualified_name,
    sync_server_tools,
)
from app.services.mcp_hub.client import (
    McpHub,
    McpHubError,
    McpServerSpec,
    normalize_call_result,
)
from app.tools.registry import get_tool, known_tools, unregister_prefix

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"

pytest.importorskip("mcp", reason="MCP SDK not installed")


def _spec(server_id: str = "cae") -> McpServerSpec:
    return McpServerSpec(
        id=server_id,
        transport="stdio",
        command=sys.executable,
        args=(str(FIXTURE),),
        startup_timeout=30.0,
        call_timeout=30.0,
    )


@pytest.fixture
async def hub():
    h = McpHub()
    yield h
    await h.close_all()
    unregister_prefix(f"{NAMESPACE}:")


async def test_lists_tools_from_a_real_server(hub):
    session = await hub.session(_spec())
    tools = {t.name: t for t in await session.list_tools()}
    assert {"set_shape", "current_shape", "solve", "always_fails"} <= set(tools)
    # schema 直接来自服务端，注册表本来就吃 JSON Schema，不需要转换
    assert tools["set_shape"].input_schema["type"] == "object"
    assert "thickness" in tools["set_shape"].input_schema["properties"]


async def test_session_is_persistent_so_server_state_survives(hub):
    """常驻会话：状态活过多次调用。每次现连的话这条断言必挂。"""
    session = await hub.session(_spec())
    await session.call_tool("set_shape", {"name": "beam", "thickness": 2.0})

    # 再取一次会话（模拟另一次工具调用）——必须是同一个会话
    again = await hub.session(_spec())
    assert again is session
    shape = await again.call_tool("current_shape", {})
    assert "beam" in str(shape)


async def test_structured_result_is_passed_through(hub):
    session = await hub.session(_spec())
    await session.call_tool("set_shape", {"name": "plate", "thickness": 4.0})
    result = await session.call_tool("solve", {"load": 8.0})
    # 求解器的数值结果必须机器可读。实测这个 SDK 把 dict 返回放进**文本块**而不填
    # structured_content，所以归一化要把 JSON 文本解析出来——否则 agent 拿到的只是
    # 一段字符串，没法据此决定下一轮怎么改设计。
    assert result.get("displacement") == 2.0
    assert result.get("converged") is True
    # 原文同时保留：人读的摘要不该因为解析成功就丢掉
    assert "displacement" in str(result.get("text", ""))


async def test_tool_failure_is_data_not_an_exception(hub):
    session = await hub.session(_spec())
    result = await session.call_tool("always_fails", {})
    # 工具自己报的失败是**结果**：agent 要能读到失败并决定下一步，而不是被异常打断
    assert result.get("is_error") is True
    # 消息内容不作更强断言：SDK 有意用通用包装（"Error executing tool ..."）而不
    # 泄露服务端的内部异常文本。能拿到「失败了 + 一句话」才是这层的契约。
    assert str(result.get("text", "")).strip()


async def test_tools_register_under_a_namespace(hub):
    names = await sync_server_tools(_spec(), hub=hub)
    assert qualified_name("cae", "solve") in names

    spec = get_tool(qualified_name("cae", "solve"))
    assert spec is not None
    # 外部工具一律按可写对待：建模/划网格/跑求解都在改外部世界
    assert spec.read_only is False
    assert spec.scope == "user"


async def test_resync_replaces_rather_than_accumulates(hub):
    first = await sync_server_tools(_spec(), hub=hub)
    second = await sync_server_tools(_spec(), hub=hub)
    assert sorted(first) == sorted(second)
    # 全量替换：同一台服务器同步两次不会留下重复或幽灵条目
    mcp_tools = [n for n in known_tools() if n.startswith(f"{NAMESPACE}:cae:")]
    assert sorted(mcp_tools) == sorted(second)


async def test_dropping_a_server_removes_exactly_its_tools(hub):
    await sync_server_tools(_spec("cad"), hub=hub)
    await sync_server_tools(_spec("mesh"), hub=hub)

    await drop_server_tools("cad", hub=hub)
    remaining = {n for n in known_tools() if n.startswith(f"{NAMESPACE}:")}
    assert all(not n.startswith(f"{NAMESPACE}:cad:") for n in remaining)
    # 摘掉一台不能误伤另一台
    assert any(n.startswith(f"{NAMESPACE}:mesh:") for n in remaining)


async def test_registered_tool_is_callable_through_the_registry(hub):
    """接进来的工具必须能被 agent 循环按注册表的常规路径调起来。"""
    await sync_server_tools(_spec(), hub=hub)
    spec = get_tool(qualified_name("cae", "set_shape"))
    assert spec is not None
    out = await spec.handler(None, {"name": "shell", "thickness": 3.0})
    assert "shell" in str(out)


async def test_bad_spec_is_rejected_before_connecting():
    with pytest.raises(McpHubError) as exc:
        McpServerSpec(id="broken", transport="stdio").validate()
    assert exc.value.code == "invalid-spec"


async def test_unreachable_server_reports_instead_of_hanging():
    hub = McpHub()
    spec = McpServerSpec(
        id="nope",
        transport="stdio",
        command=sys.executable,
        args=("-c", "import sys; sys.exit(1)"),
        startup_timeout=10.0,
    )
    with pytest.raises(McpHubError) as exc:
        await hub.session(spec)
    # 连不上要给出可分流的码，而不是卡在那里等
    assert exc.value.code in {"connect-failed", "startup-timeout"}
    await hub.close_all()


def test_normalize_prefers_structured_but_keeps_text():
    class _Block:
        text = "human readable"

    class _Result:
        structured_content = {"value": 1}
        content = [_Block()]
        is_error = False

    payload = normalize_call_result(_Result())
    assert payload["value"] == 1
    assert payload["text"] == "human readable"
