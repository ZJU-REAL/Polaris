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


def test_the_default_surface_is_every_read_only_tool_minus_a_named_few():
    """默认给**全部只读工具**，例外要显式列出来。

    这里原来钉的是相反的判断（「只给 6 个检索工具，schema 每轮重发太贵」）。那个判断
    错在两处：代价被高估——全部只读工具的 schema 约 3.2k tokens，比一轮问答的历史还小；
    代价也算错了地方——模型拿不到取图工具时不会「省下一次调用」，而是假装看过图，或者
    干脆说做不到。用户为此付的远比几千 token 贵。

    动态取而不是写死名单：新工具加进注册表就自动可用。写死的问题不是麻烦，而是没人
    会记得回来加，助手会悄悄少一项能力而界面上看不出异常。
    """
    from app.agents.chat.prompt import _TOOL_DENYLIST, default_tool_names
    from app.tools.memory import MEMORY_TOOL_NAMES
    from app.tools.registry import list_tools

    names = set(default_tool_names())
    read_only = {spec.name for spec in list_tools() if spec.read_only}

    # 记忆没打开时 remember/recall 不在工具面里——模型不知道有这回事，也就不会假装
    # 记住；「我记下了」而其实没存，比不能记更糟
    assert names == read_only - _TOOL_DENYLIST - set(MEMORY_TOOL_NAMES)
    assert not set(MEMORY_TOOL_NAMES) & names

    # 打开之后两个都在，且 remember 是工具面里唯一会写的
    with_memory = set(default_tool_names(memory_enabled=True))
    assert set(MEMORY_TOOL_NAMES) <= with_memory
    writers = {spec.name for spec in list_tools() if not spec.read_only}
    assert with_memory & writers == {"remember"}
    # 用户实际撞上的那几个：取图、读解读、看实验
    assert {"get_paper_figure", "read_wiki", "get_experiment"} <= names
    # 写工具一个都不能混进来（本期仍是只读期）
    assert not {spec.name for spec in list_tools() if not spec.read_only} & names


def test_the_denylist_says_why_each_entry_is_out():
    """例外必须少而有据：get_fact_pack 一次返回整包事实，模型总能用更便宜的工具问到。"""
    from app.agents.chat.prompt import _TOOL_DENYLIST

    assert set(_TOOL_DENYLIST) == {"get_fact_pack"}


@pytest.mark.asyncio
async def test_meta_is_the_first_event(monkeypatch):
    """首帧永远是 meta：前端据此绑定会话与消息 id。"""
    provider = FakeProvider()
    provider.script(["直接回答。"])
    events = await _drive(_loop(provider))
    assert isinstance(events[0], MetaEvent)
    assert events[0].conversation_id


def test_paper_refs_are_found_wherever_they_sit_in_the_payload():
    """论文引用走通用遍历，不跟每个工具的形状耦合。

    逐个工具适配的话，每加一个工具就得记得回来改；漏改的表现是「列表少了几篇」，
    没人会注意到。
    """
    from app.agents.chat.loop import _paper_refs

    refs = _paper_refs(
        {
            "results": [{"paper_id": "a", "title": "甲", "score": 1}],
            "nested": {"deep": [{"papers": [{"paper_id": "b", "title": "乙"}]}]},
            "single": {"paper_id": "c", "title": "丙"},
        }
    )
    assert [r["paper_id"] for r in refs] == ["a", "b", "c"]


def test_paper_refs_skip_entries_without_a_title_and_dedupe():
    """只收同时有 id 和标题的：光有 uuid 的条目在界面上没法看。"""
    from app.agents.chat.loop import _paper_refs

    refs = _paper_refs(
        {
            "a": {"paper_id": "x", "title": "标题"},
            "b": {"paper_id": "x", "title": "同一篇又出现一次"},
            "c": {"paper_id": "y"},  # 没标题
            "d": {"title": "没 id"},
            "e": {"paper_id": "z", "title": "   "},  # 空白标题不算
        }
    )
    assert [r["paper_id"] for r in refs] == ["x"]


def test_paper_refs_are_capped():
    """scan_papers 一次能回 50 篇，全铺上去这一栏就成了第二个搜索结果页。"""
    from app.agents.chat.loop import _MAX_SOURCES, _paper_refs

    payload = {"results": [{"paper_id": str(i), "title": f"第 {i} 篇"} for i in range(80)]}
    assert len(_paper_refs(payload)) == _MAX_SOURCES


@pytest.mark.asyncio
async def test_the_loop_reports_papers_it_looked_at(monkeypatch):
    """整条链路：工具查到论文 → 循环播一帧 sources，同一篇不重复播。"""

    @tool(
        name="_probe_papers",
        description="测试用检索",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    async def _probe(ctx, args):  # noqa: ANN001
        return {
            "results": [
                {"paper_id": "p-1", "title": "第一篇"},
                {"paper_id": "p-2", "title": "第二篇"},
            ]
        }

    provider = FakeProvider()
    provider.script(
        [
            [("_probe_papers", {"q": "planning"})],
            # 第二轮又查了一次，返回同样的两篇：不该再播一遍
            [("_probe_papers", {"q": "planning 换个说法"})],
            "查到两篇。",
        ]
    )
    events = await _drive(_loop(provider), tool_names=("_probe_papers",))
    sources = [e for e in events if type(e).__name__ == "SourcesEvent"]

    assert len(sources) == 1, f"同一批论文被播了 {len(sources)} 次"
    assert [i["paper_id"] for i in sources[0].items] == ["p-1", "p-2"]
    assert [i["title"] for i in sources[0].items] == ["第一篇", "第二篇"]
