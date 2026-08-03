"""子智能体 + agentic search 的两个原语，以及技能收窄的接线。

这三件事对齐的是 Claude Code 里最实际的两条：**宽度与深度分两轮**，以及**派子 agent
去烧它自己的上下文**。
"""

import json
import uuid

import pytest

from app.agents.chat.events import DeltaEvent
from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.tools.context import ToolContext
from app.tools.registry import get_tool, tool


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    """测试注册的工具用完即撤——@tool 写的是全局注册表，泄漏出去会进 MCP 的清单。"""
    from app.tools import registry

    before = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


class _ScriptedRouter(LLMRouter):
    def __init__(self, provider: FakeProvider) -> None:
        super().__init__()
        self._provider = provider

    async def stream_events(self, stage, messages, **kwargs):  # noqa: ANN001, ANN003
        self.last_tools = [t["name"] for t in (kwargs.get("tools") or [])]
        async for ev in self._provider.stream_events(
            messages,
            model="fake",
            tools=kwargs.get("tools"),
            tool_choice=kwargs.get("tool_choice"),
        ):
            yield ev


def _loop(provider: FakeProvider, **kw):
    ctx = ToolContext(project_id=uuid.uuid4(), llm=LLMRouter(), user_id=uuid.uuid4())
    return ChatAgentLoop(llm=_ScriptedRouter(provider), tool_ctx=ctx, **kw)


# ---- agentic search：宽扫刻意不带摘要 ----


def test_scan_returns_one_line_per_paper_not_snippets():
    """宽扫每篇只给标题和年份。

    回了摘要就又变成 top-k RAG——k 也就不敢往大了要，而"能一次看 50 篇"正是它存在的
    理由。这条用 schema 与描述来钉：它承诺的就是便宜。
    """
    spec = get_tool("scan_papers")
    assert spec is not None and spec.read_only
    props = spec.input_schema["properties"]
    assert props["k"]["type"] == "integer"
    assert "不给摘要" in spec.description
    assert "50" in props["k"]["description"]


def test_grep_returns_excerpts_not_whole_chunks():
    """grep 回命中处前后一小段，不是整个 chunk。"""
    from app.tools.agentic_search import _GREP_WINDOW, _matching_windows

    text = "前面很多字。" * 50 + "这里出现了 GRPO 这个词。" + "后面也很多字。" * 50
    hits = _matching_windows(text, "grpo")
    assert len(hits) == 1
    assert "GRPO" in hits[0]
    assert len(hits[0]) <= _GREP_WINDOW + 40, "不能把整段回过去"

    assert _matching_windows("完全无关的文本", "grpo") == []


# ---- 子 agent：中间结果不进父上下文 ----


@pytest.mark.asyncio
async def test_subagent_keeps_its_intermediate_results_out_of_the_parent():
    """父那边只看到一条结论，子 agent 扫过的东西不进父历史。

    这是子 agent 存在的**全部理由**：主 agent 自己扫 40 篇的话，那 40 条结果此后每轮
    都要重发，几轮之内上下文就被搜索结果占满。
    """
    scanned: list[str] = []

    # 先摘掉真工具再放替身：注册表拒绝重名（这是对的，重名注册一定是 bug），
    # 而 fixture 会在测试结束时把整张表还原。
    from app.tools import registry as registry_mod

    registry_mod._REGISTRY.pop("scan_papers", None)

    @tool(
        name="scan_papers",  # 替身：避免碰数据库
        description="宽扫",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    async def _scan(ctx, args):  # noqa: ANN001
        scanned.append(args.get("query", ""))
        # 故意回一大坨：如果它进了父上下文，下面的断言会抓到
        return {
            "results": [
                {"paper_id": str(uuid.uuid4()), "title": f"论文 {i}"} for i in range(40)
            ]
        }

    # 子 agent 内部：先扫一次，再给结论
    child = FakeProvider()
    child.script([[("scan_papers", {"query": "planning"})], "扫了 40 篇，主流是树搜索。"])

    from app.core.llm import router as router_mod
    from app.tools import subagent as subagent_mod

    # 让子 agent 用脚本化的 provider
    original = router_mod.get_llm_router
    router_mod.get_llm_router = lambda: _ScriptedRouter(child)  # type: ignore[assignment]
    try:
        ctx = ToolContext(project_id=uuid.uuid4(), llm=LLMRouter(), user_id=uuid.uuid4())
        result = await subagent_mod.run_subagent(ctx, {"task": "梳理 planning 的相关工作"})
    finally:
        router_mod.get_llm_router = original  # type: ignore[assignment]

    assert scanned == ["planning"], "子 agent 真的去查了"
    assert "树搜索" in result["conclusion"]
    assert result["tool_calls"] == 1
    # 结论里不该夹带 40 条中间结果
    blob = json.dumps(result, ensure_ascii=False)
    assert blob.count("论文 ") <= 1, "中间结果不能跟着结论回到父上下文"
    assert len(blob) < 2000, f"回给父的东西必须是小的，实际 {len(blob)}"


@pytest.mark.asyncio
async def test_subagent_cannot_spawn_another_subagent():
    """子 agent 的工具清单里没有 run_subagent——递归派发会炸开且父完全看不见。"""
    from app.tools.subagent import SUBAGENT_TOOLS

    assert "run_subagent" not in SUBAGENT_TOOLS
    assert "scan_papers" in SUBAGENT_TOOLS and "read_fulltext" in SUBAGENT_TOOLS


@pytest.mark.asyncio
async def test_subagent_reports_a_missing_task_instead_of_running():
    ctx = ToolContext(project_id=uuid.uuid4(), llm=LLMRouter(), user_id=uuid.uuid4())
    from app.tools.subagent import run_subagent

    assert "error" in await run_subagent(ctx, {"task": "  "})


# ---- 技能收窄：接线是否真的生效 ----


@pytest.mark.asyncio
async def test_a_loaded_skill_narrows_the_tools_for_later_rounds():
    """技能声明 allowed-tools 之后，后续轮次的工具清单真的变窄了。

    之前 narrow_tools 写好了但**没接进循环**，所以 allowed-tools 只是装饰。
    """
    @tool(
        name="_fake_skill_load",
        description="加载技能",
        input_schema={"type": "object", "properties": {}},
    )
    async def _load(ctx, args):  # noqa: ANN001
        return {"slug": "triage", "body": "...", "allowed_tools": ["_probe_keep"]}

    @tool(name="_probe_keep", description="留下", input_schema={"type": "object", "properties": {}})
    async def _keep(ctx, args):  # noqa: ANN001
        return {"ok": True}

    @tool(
        name="_probe_drop",
        description="被收窄掉",
        input_schema={"type": "object", "properties": {}},
    )
    async def _drop(ctx, args):  # noqa: ANN001
        return {"ok": True}

    provider = FakeProvider()
    provider.script([[("_fake_skill_load", {})], [("_probe_keep", {})], "好了。"])
    loop = _loop(provider)
    router = loop._llm  # type: ignore[attr-defined]

    events = [
        ev
        async for ev in loop.run(
            ChatTurnRequest(
                conversation_id=uuid.uuid4(),
                question="q",
                tool_names=("_fake_skill_load", "_probe_keep", "_probe_drop"),
            )
        )
    ]

    assert any(isinstance(e, DeltaEvent) for e in events)
    # 最后一轮送给模型的工具清单里，被技能排除的那个已经没了
    assert "_probe_keep" in router.last_tools
    assert "_probe_drop" not in router.last_tools, "技能的 allowed-tools 没有生效"


@pytest.mark.asyncio
async def test_a_skill_cannot_widen_the_tool_set():
    """技能里写一个会话没给的工具名，不会把它加进来。"""
    @tool(
        name="_fake_skill_greedy",
        description="贪心技能",
        input_schema={"type": "object", "properties": {}},
    )
    async def _greedy(ctx, args):  # noqa: ANN001
        return {"allowed_tools": ["_probe_keep", "delete_everything"]}

    @tool(name="_probe_keep", description="留下", input_schema={"type": "object", "properties": {}})
    async def _keep(ctx, args):  # noqa: ANN001
        return {"ok": True}

    provider = FakeProvider()
    provider.script([[("_fake_skill_greedy", {})], "好了。"])
    loop = _loop(provider)
    router = loop._llm  # type: ignore[attr-defined]

    async for _ in loop.run(
        ChatTurnRequest(
            conversation_id=uuid.uuid4(),
            question="q",
            tool_names=("_fake_skill_greedy", "_probe_keep"),
        )
    ):
        pass

    assert "delete_everything" not in router.last_tools
    assert "_probe_keep" in router.last_tools


@pytest.mark.asyncio
async def test_a_skill_with_a_wrong_tool_name_does_not_mute_the_assistant():
    """技能把工具名写错、交集为空时不生效——否则助手会突然变成哑巴。"""
    @tool(
        name="_fake_skill_typo",
        description="写错名字的技能",
        input_schema={"type": "object", "properties": {}},
    )
    async def _typo(ctx, args):  # noqa: ANN001
        return {"allowed_tools": ["serch_papers"]}  # 拼错

    provider = FakeProvider()
    provider.script([[("_fake_skill_typo", {})], "好了。"])
    loop = _loop(provider)
    router = loop._llm  # type: ignore[attr-defined]

    async for _ in loop.run(
        ChatTurnRequest(
            conversation_id=uuid.uuid4(), question="q", tool_names=("_fake_skill_typo",)
        )
    ):
        pass

    assert router.last_tools, "交集为空时应保持原样，而不是把工具全砍光"
