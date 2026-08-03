"""对话 agent 循环：事件序列、并行调用、错误恢复、轮次与预算收尾。

循环不 import fastapi，所以这里脱离 HTTP 直接驱动它——脚本化的 FakeProvider 排好
"这轮调这些工具、下轮直接作答"，整条路径就能跑穿。
"""

import uuid

import pytest

from app.agents.chat.events import (
    CompactionEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
from app.agents.chat.prompt import DEFAULT_TOOL_NAMES, build_system_prompt, tool_definitions
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.tools.context import ToolContext
from app.tools.registry import tool


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    """测试注册的工具用完即撤。

    ``@tool`` 写的是**模块级全局注册表**，而 MCP 的 tools/list 无过滤地遍历它——
    泄漏出去的假工具会出现在外部 MCP 客户端的工具清单里，自检也会把故意抛异常的那个
    判成"已失效"。（这条正是 MCP 那侧缺一道只读/白名单过滤的证据，引入写工具时必须一并修。）
    """
    from app.tools import registry

    before = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


class _ScriptedRouter(LLMRouter):
    """只替换 stream_events 的路由器：不碰数据库配置，直接用脚本化的假 provider。"""

    def __init__(self, provider: FakeProvider) -> None:
        super().__init__()
        self._provider = provider

    async def stream_events(self, stage, messages, **kwargs):  # noqa: ANN001, ANN003
        async for ev in self._provider.stream_events(
            messages,
            model="fake",
            tools=kwargs.get("tools"),
            tool_choice=kwargs.get("tool_choice"),
        ):
            yield ev


def _loop(provider: FakeProvider) -> ChatAgentLoop:
    ctx = ToolContext(project_id=uuid.uuid4(), llm=LLMRouter(), user_id=uuid.uuid4())
    return ChatAgentLoop(llm=_ScriptedRouter(provider), tool_ctx=ctx)


async def _drive(loop: ChatAgentLoop, **kwargs):
    req = ChatTurnRequest(conversation_id=uuid.uuid4(), question="问题", **kwargs)
    return [ev async for ev in loop.run(req)]


# ---- 事件序列 ----


@pytest.mark.asyncio
async def test_the_happy_path_emits_meta_tool_call_result_delta_done(monkeypatch):
    """一轮完整的工具调用：meta → tool_call → tool_result → delta* → done。"""
    calls: list[dict] = []

    @tool(
        name="_probe_search",
        description="测试用检索",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        summarize=lambda args, result: f"检索 {args.get('query')} → {result.get('hits')} 条",
    )
    async def _probe(ctx, args):  # noqa: ANN001
        calls.append(args)
        return {"hits": 3, "titles": ["A", "B", "C"]}

    provider = FakeProvider()
    provider.script([[("_probe_search", {"query": "planning"})], "查到三篇，主流是树搜索。"])

    events = await _drive(_loop(provider), tool_names=("_probe_search",))
    kinds = [type(e).__name__ for e in events]

    assert kinds[0] == "MetaEvent"
    # tool_call 必须在 tool_result **之前**：前端靠它画出 running 卡片，
    # 否则几秒的等待期里界面上什么都没有，卡片跑完才凭空出现在完成态
    assert kinds.index("ToolCallEvent") < kinds.index("ToolResultEvent")
    assert kinds[-1] == "DoneEvent"
    assert calls == [{"query": "planning"}], "工具真的被调到了，参数完整"

    call_ev = next(e for e in events if isinstance(e, ToolCallEvent))
    assert call_ev.name == "_probe_search" and call_ev.args == {"query": "planning"}
    assert call_ev.read_only is True

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is True
    assert result.summary == "检索 planning → 3 条", "摘要来自 ToolSpec.summarize"
    assert '"hits": 3' in result.preview

    answer = "".join(e.text for e in events if isinstance(e, DeltaEvent))
    assert "树搜索" in answer
    done = events[-1]
    assert isinstance(done, DoneEvent) and done.stop_reason == "stop"


@pytest.mark.asyncio
async def test_parallel_calls_all_execute():
    """一轮里的多个调用并行执行，每个都产出一条结果事件。"""
    seen: list[str] = []

    @tool(
        name="_probe_a",
        description="a",
        input_schema={"type": "object", "properties": {}},
    )
    async def _a(ctx, args):  # noqa: ANN001
        seen.append("a")
        return {"ok": "a"}

    @tool(
        name="_probe_b",
        description="b",
        input_schema={"type": "object", "properties": {}},
    )
    async def _b(ctx, args):  # noqa: ANN001
        seen.append("b")
        return {"ok": "b"}

    provider = FakeProvider()
    provider.script([[("_probe_a", {}), ("_probe_b", {})], "都查完了。"])
    events = await _drive(_loop(provider), tool_names=("_probe_a", "_probe_b"))

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert {r.name for r in results} == {"_probe_a", "_probe_b"}
    assert sorted(seen) == ["a", "b"]


# ---- 错误恢复：每一级都不杀死本轮 ----


@pytest.mark.asyncio
async def test_a_tool_that_raises_becomes_a_result_and_the_round_continues():
    """工具抛异常转成错误结果回喂，循环继续——模型下一轮可以换个说法再试。"""

    @tool(name="_probe_boom", description="炸", input_schema={"type": "object", "properties": {}})
    async def _boom(ctx, args):  # noqa: ANN001
        raise RuntimeError("后端挂了")

    provider = FakeProvider()
    provider.script([[("_probe_boom", {})], "工具没查到，我先说我知道的。"])
    events = await _drive(_loop(provider), tool_names=("_probe_boom",))

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False and "后端挂了" in result.preview
    assert isinstance(events[-1], DoneEvent), "这轮仍然正常收尾"
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_an_unknown_tool_is_reported_back_not_raised():
    """模型编了个不存在的工具名：告诉它，别崩。"""
    provider = FakeProvider()
    provider.script([[("_probe_nonexistent", {})], "换个方式回答。"])
    events = await _drive(_loop(provider), tool_names=("search_papers",))

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False and "未知工具" in result.summary
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_malformed_arguments_are_reported_back():
    """参数不是合法 JSON 时不执行工具，回一条让模型重发的结果。

    弱模型上这是高频情形；累加器把它标成 __parse_error__ 传下来。
    """
    from app.core.llm.base import StreamDone, ToolUseArgsDelta, ToolUseStart

    class _BadArgs(FakeProvider):
        async def stream_events(self, messages, **kwargs):  # noqa: ANN001, ANN003
            if kwargs.get("tool_choice") == "none" or getattr(self, "_done", False):
                async for ev in super().stream_events(messages, **kwargs):
                    yield ev
                return
            self._done = True
            yield ToolUseStart(0, "c1", "search_papers")
            yield ToolUseArgsDelta(0, '{"query": ')  # 截断
            yield StreamDone(finish_reason="tool_use")

    provider = _BadArgs()
    provider.script([None, "那我直接答。"])  # 第二轮走父类的文本
    events = await _drive(_loop(provider), tool_names=("search_papers",))

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False and "合法 JSON" in result.summary


@pytest.mark.asyncio
async def test_a_provider_error_ends_the_turn_with_an_error_event():
    """provider 挂了产出 error 事件，而不是把异常抛给 HTTP 层。"""

    class _Broken(FakeProvider):
        async def stream_events(self, messages, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("连接被重置")
            yield  # pragma: no cover

    events = await _drive(_loop(_Broken()))
    assert isinstance(events[-1], ErrorEvent)
    assert "连接被重置" in events[-1].detail


@pytest.mark.asyncio
async def test_tools_unsupported_is_recoverable_so_callers_can_degrade():
    """中转不认 tools 时标成可恢复——调用方据此退回一次性 RAG，而不是报错。"""
    from app.core.llm.base import ToolsUnsupportedError

    class _NoTools(FakeProvider):
        async def stream_events(self, messages, **kwargs):  # noqa: ANN001, ANN003
            raise ToolsUnsupportedError("this relay does not accept tools")
            yield  # pragma: no cover

    events = await _drive(_loop(_NoTools()))
    err = events[-1]
    assert isinstance(err, ErrorEvent)
    assert err.code == "TOOLS_UNSUPPORTED" and err.recoverable is True


# ---- 收尾条件 ----


@pytest.mark.asyncio
async def test_running_out_of_rounds_hard_stops_the_tools():
    """轮次耗尽用 tool_choice="none" 硬关工具，不是追加 user 消息哄模型收尾。"""

    @tool(
        name="_probe_loopy",
        description="loop",
        input_schema={"type": "object", "properties": {}},
    )
    async def _loopy(ctx, args):  # noqa: ANN001
        return {"again": True}

    provider = FakeProvider()
    # 剧本一直想调工具；到顶那轮必须被 tool_choice=none 挡下来
    provider.script([[("_probe_loopy", {})]] * 10)

    events = await _drive(_loop(provider), tool_names=("_probe_loopy",), max_rounds=3)
    done = events[-1]
    assert isinstance(done, DoneEvent) and done.stop_reason == "max_rounds"
    assert len([e for e in events if isinstance(e, ToolResultEvent)]) == 2, "第 3 轮不再调工具"


@pytest.mark.asyncio
async def test_old_tool_results_get_elided():
    """较早的工具结果压成一行——它们是上下文里的大头。"""

    @tool(name="_probe_big", description="big", input_schema={"type": "object", "properties": {}})
    async def _big(ctx, args):  # noqa: ANN001
        return {"text": "x" * 3000}

    provider = FakeProvider()
    provider.script([[("_probe_big", {})]] * 4 + ["够了。"])
    events = await _drive(_loop(provider), tool_names=("_probe_big",), max_rounds=6)
    assert any(isinstance(e, CompactionEvent) for e in events), "第 3 轮之后应触发驱逐"


# ---- 提示与工具定义 ----


def test_tool_definitions_are_plain_json_schema():
    """ToolSpec.input_schema 直接就是两家要的 parameters/input_schema，零转换。"""
    defs = tool_definitions()
    assert {d["name"] for d in defs} == set(DEFAULT_TOOL_NAMES)
    for d in defs:
        assert d["parameters"]["type"] == "object"
        assert isinstance(d["description"], str) and d["description"]


def test_system_prompt_keeps_a_stable_prefix():
    """方向说明与技能追加在**末尾**：前缀不变才吃得到 prompt cache。

    顺序反过来的话，每个作用域都有自己的前缀，缓存命中率归零。
    """
    base = build_system_prompt()
    with_statement = build_system_prompt("Computer-use agents")
    assert with_statement.startswith(base.split("今天是")[0])
    assert "Computer-use agents" in with_statement
    assert with_statement.index("Computer-use agents") > with_statement.index("你是 Polaris")


def test_default_tool_whitelist_is_small():
    """首期只给 6 个。38 个 schema 每轮重发既贵，又让模型在相近工具间反复犹豫。"""
    assert len(DEFAULT_TOOL_NAMES) == 6
    assert "search_chunks" in DEFAULT_TOOL_NAMES


@pytest.mark.asyncio
async def test_meta_is_the_first_event(monkeypatch):
    """首帧永远是 meta：前端据此绑定会话与消息 id。"""
    provider = FakeProvider()
    provider.script(["直接回答。"])
    events = await _drive(_loop(provider))
    assert isinstance(events[0], MetaEvent)
    assert events[0].conversation_id
