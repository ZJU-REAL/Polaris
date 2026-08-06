"""router 侧的工具调用：新 stage、耐心档位、tools 透传、记账，以及向后兼容。"""

import pytest

from app.core.llm.base import (
    LLMProvider,
    Message,
    StreamDone,
    TextDelta,
    ToolUseBlock,
    ToolUseStart,
)
from app.core.llm.fake import FakeProvider
from app.core.llm.router import _STAGE_ROUTE_FALLBACKS, STAGES, call_profile
from app.core.llm.tool_stream import ToolCallAccumulator


def test_agent_stage_exists_and_falls_back_to_reading():
    """新 stage 未配置时继承 reading 的路由——存量部署不用改任何配置就能跑。"""
    assert "agent" in STAGES
    assert _STAGE_ROUTE_FALLBACKS["agent"] == "reading"


def test_agent_gets_its_own_patience_not_the_long_profile():
    """agent 单独一档，既不是短档也不是长档。

    短档（60s×2）不够想完一轮；长档（300s×4）最坏 20 分钟攥着一个 HTTP 连接不放，
    而对话是同步的——用户早走了。
    """
    agent = call_profile("agent")
    assert agent == (180.0, 2)
    assert agent != call_profile("relevance"), "不能是短档"
    assert agent != call_profile("librarian"), "也不能是长档"
    # reading（现有对话）必须保持短档不变
    assert call_profile("reading") == call_profile("relevance")


@pytest.mark.asyncio
async def test_a_provider_without_tool_support_still_streams():
    """没重写 stream_events 的 provider 走默认实现，行为与从前一致。

    这条是整个改造能安全落地的关键：测试里那些手写替身一行没改，仍然能用。
    """

    class OldStyleProvider(LLMProvider):
        name = "old"

        async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
            raise NotImplementedError

        async def stream(self, messages, **kwargs):  # noqa: ANN001, ANN003
            for piece in ("你", "好"):
                yield piece

    provider = OldStyleProvider()
    assert provider.supports_tools is False
    events = [
        ev
        async for ev in provider.stream_events([Message(role="user", content="hi")], model="m")
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["你", "好"]
    assert isinstance(events[-1], StreamDone)


@pytest.mark.asyncio
async def test_fake_provider_without_a_script_behaves_exactly_as_before():
    """不排剧本时假 provider 不发任何工具调用。

    现存 900 多条测试全靠这一点——它们从没听说过工具。
    """
    provider = FakeProvider()
    events = [
        ev
        async for ev in provider.stream_events(
            [Message(role="user", content="随便问点什么")], model="m"
        )
    ]
    assert not any(isinstance(e, ToolUseStart) for e in events)
    assert any(isinstance(e, TextDelta) for e in events)


@pytest.mark.asyncio
async def test_scripted_fake_drives_parallel_tool_calls():
    """排了剧本就按剧本发起并行调用，参数能被累加器还原。"""
    provider = FakeProvider()
    provider.script(
        [
            [("search_papers", {"query": "planning", "k": 5}), ("get_paper", {"paper_id": "p1"})],
            "根据检索结果，主流方法是树搜索。",
        ]
    )

    acc = ToolCallAccumulator()
    async for ev in provider.stream_events([Message(role="user", content="q")], model="m"):
        acc.feed(ev)
    calls = [b for b in acc.finish() if isinstance(b, ToolUseBlock)]
    assert [c.name for c in calls] == ["search_papers", "get_paper"]
    assert calls[0].input == {"query": "planning", "k": 5}
    assert calls[1].input == {"paper_id": "p1"}

    # 第二轮：剧本给的是一段文本，不再发工具
    acc2 = ToolCallAccumulator()
    async for ev in provider.stream_events([Message(role="user", content="q")], model="m"):
        acc2.feed(ev)
    assert not acc2.has_calls
    assert "树搜索" in acc2.text


@pytest.mark.asyncio
async def test_tool_choice_none_hard_stops_tool_calls():
    """``tool_choice="none"`` 时一律不发工具。

    agent 循环轮次耗尽后就是靠它硬关工具收尾的（而不是追加 user 消息哄模型），
    所以这条语义在假 provider 上也必须成立，否则那条路径没人测。
    """
    provider = FakeProvider()
    provider.script([[("search_papers", {"query": "x"})]])
    events = [
        ev
        async for ev in provider.stream_events(
            [Message(role="user", content="q")], model="m", tool_choice="none"
        )
    ]
    assert not any(isinstance(e, ToolUseStart) for e in events)


@pytest.mark.asyncio
async def test_marker_drives_tools_without_a_provider_handle():
    """端到端路径拿不到 provider 实例，用消息里的标记驱动。"""
    provider = FakeProvider()
    acc = ToolCallAccumulator()
    async for ev in provider.stream_events(
        [Message(role="user", content='请查一下\nPOLARIS_FAKE_TOOL:search_papers:{"query": "x"}')],
        model="m",
    ):
        acc.feed(ev)
    calls = [b for b in acc.finish() if isinstance(b, ToolUseBlock)]
    assert len(calls) == 1 and calls[0].name == "search_papers"
    assert calls[0].input == {"query": "x"}


def test_message_text_flattens_blocks_and_keeps_constructors_working():
    """``.text`` 是 12 处读取点的新落点；71 处构造点一行未动。"""
    from app.core.llm.base import TextBlock, ToolResultBlock

    plain = Message(role="user", content="就是一段字符串")
    assert plain.text == "就是一段字符串"
    assert plain.blocks == (TextBlock("就是一段字符串"),)

    mixed = Message(
        role="assistant",
        content=[TextBlock("我查一下"), ToolUseBlock("c1", "search_papers", {"q": "x"})],
    )
    assert "我查一下" in mixed.text
    assert "search_papers" in mixed.text, "工具调用在拍平时要留下痕迹，不能凭空消失"

    result = Message(role="user", content=[ToolResultBlock("c1", '{"hits": 3}')])
    assert "hits" in result.text


def test_completion_result_rebuilds_the_assistant_message_with_blocks():
    """回喂历史要用 as_assistant_message，不能拿 content 手工拼——那样会丢块。"""
    from app.core.llm.base import CompletionResult, TextBlock

    result = CompletionResult(
        content="我查一下",
        model="m",
        blocks=(TextBlock("我查一下"), ToolUseBlock("c1", "search_papers", {"q": "x"})),
    )
    assert [c.name for c in result.tool_calls] == ["search_papers"]
    msg = result.as_assistant_message()
    assert isinstance(msg.content, list) and len(msg.content) == 2

    # 没有块时退回纯文本，与从前一致
    plain = CompletionResult(content="hi", model="m")
    assert plain.as_assistant_message().content == "hi"
    assert plain.tool_calls == ()


def test_provider_errors_keep_the_upstream_body():
    """400 的原因写在上游的响应体里，不能只把状态码抛出去。

    线上就撞过：用户只看到「HTTPStatusError: 400 Bad Request」，而中转其实已经写明了
    是哪个参数不合法——那行字被 raise_for_status() 丢掉了，我们和用户都无从查起。
    """
    import inspect

    from app.core.llm import openai_compat

    src = inspect.getsource(openai_compat.OpenAICompatProvider.stream_events)
    assert "raise_for_status" not in src, "流式错误路径不能用 raise_for_status（它丢正文）"


def test_tools_unsupported_is_detected_from_the_body():
    """不认 tools 的说法五花八门，命中就该降级而不是失败。"""
    from app.core.llm.openai_compat import _tools_unsupported

    assert _tools_unsupported('{"error":{"message":"tools is not supported for this model"}}')
    assert _tools_unsupported("Unsupported parameter: 'tools'")
    assert not _tools_unsupported('{"error":{"message":"context length exceeded"}}')
