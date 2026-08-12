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
from app.core.llm.router import (
    _LONG_CALL_STAGES,
    _MEDIUM_CALL_STAGES,
    _SHORT_CALL_STAGES,
    STAGES,
    STREAM_STAGES,
    call_profile,
)
from app.core.llm.tool_stream import ToolCallAccumulator


def test_agent_stage_exists():
    """agent 是个可单独配路由的环节；没配时和别的环节一样跟随 default。"""
    assert "agent" in STAGES


def test_medium_stages_get_more_patience_without_the_long_profile():
    """代理、目标构建和方案评审使用中档，既不是短档也不是长档。

    短档（60s×2）不够想完一轮；长档（300s×4）最坏 20 分钟攥着一个 HTTP 连接不放，
    而对话是同步的——用户早走了。
    """
    for stage in ("agent", "goal_explore", "proposal_review"):
        profile = call_profile(stage)
        assert profile == (180.0, 2)
        assert profile != call_profile("relevance"), "不能是短档"
        assert profile != call_profile("librarian"), "也不能是长档"
    # reading（现有对话）必须保持短档不变
    assert call_profile("reading") == call_profile("relevance")


def test_every_known_stage_has_exactly_one_call_profile():
    """新增 stage 时必须显式归类，避免长任务静默回落到 60 秒短档。

    这里一度写着 ``classified - known == {"digest"}``，注解是「仅允许已知的内部
    profile key 不公开为路由」。那不是不变量，是当时 digest 漏进 ``STAGES`` 的
    bug 被顺手固化了——digest 在设置页上一直是公开可配的一行，只是后端不认，
    管理员一配整张路由表就存不进去。断言把这个错状态钉成了「约定」，谁去修
    ``STAGES`` 都会先被它拦下，还以为自己破坏了什么。

    档位集合里不该出现 ``STAGES`` 之外的名字：那种名字要么是拼错的（对应的环节
    因此静默落回短档），要么就是又一个该公开却没公开的环节。
    """
    known = set(STAGES)
    short = set(_SHORT_CALL_STAGES)
    medium = set(_MEDIUM_CALL_STAGES)
    long = set(_LONG_CALL_STAGES)

    classified = short | medium | long
    assert known <= classified, f"这些环节没归档位：{sorted(known - classified)}"
    assert classified <= known, f"档位集合里有不存在的环节：{sorted(classified - known)}"
    assert short.isdisjoint(medium)
    assert short.isdisjoint(long)
    assert medium.isdisjoint(long)
    assert set(STREAM_STAGES) <= long


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


def test_provider_cache_key_covers_every_client_affecting_field():
    """会改变客户端行为的字段必须全在缓存键里。

    漏一个的表现是：两份配置共用同一个 HTTP 客户端，后配的那份静默用着前一份的
    连接参数——base_url 指向 A、请求却发去 B，日志里两边都看不出问题。user_agent
    加进来时键跟着加了，这条用来保证下一个字段不会漏。
    """
    import inspect

    from app.core.llm import router as router_mod

    src = inspect.getsource(router_mod.LLMRouter._provider_for)
    key_block = src.split("key = (", 1)[1].split(")", 1)[0]
    for field in ("provider_kind", "base_url", "api_key", "user_agent"):
        assert f"route.{field}" in key_block, f"缓存键漏了 route.{field}"
    assert "timeout" in key_block and "attempts" in key_block, "耐心档位也要进键"


def test_provider_cache_key_annotation_matches_the_real_key():
    """键的类型标注要和实际构造的键等长，否则标注是错的还没人发现。"""
    import typing

    from app.core.llm.router import _ProviderKey

    key_type = typing.get_args(typing.get_args(_ProviderKey)[0])
    assert len(key_type) == 6, f"标注是 {len(key_type)} 元组，实际键是 6 元组"
