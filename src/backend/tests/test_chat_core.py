"""对话循环内核：Navigator / Helm / Sextant。

拆成三件套的价值就在这个文件里——Navigator 不碰 IO、Sextant 不调模型，于是
「还剩几轮」「引用编号跑没跑偏」这类判断可以脱离一切外部依赖直接钉住。
"""

import asyncio
import json
import uuid

import pytest

from app.agents.chat.core import Helm, Navigator, Sextant
from app.core.llm.base import ToolResultBlock, ToolUseBlock
from app.tools.context import ToolContext
from app.tools.registry import tool


@pytest.fixture(autouse=True)
def _clean_registry():
    """@tool 注册进模块级全局表，测试注册的工具会漏进 MCP 的 tools/list。"""
    from app.tools import registry

    before = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


# ---- Navigator ----


def test_navigator_hard_closes_tools_on_the_last_round():
    """到顶就硬关工具，而不是追加一条 user 消息哄模型收手——那是在请求它配合。"""
    nav = Navigator(max_rounds=3)
    assert nav.plan_round(rounds_done=1, tokens_used=0).tool_choice is None
    assert nav.plan_round(rounds_done=3, tokens_used=0).tool_choice == "none"


def test_navigator_stops_on_token_budget_even_mid_way():
    nav = Navigator(max_rounds=8, max_turn_tokens=1000)
    plan = nav.plan_round(rounds_done=2, tokens_used=1200)
    assert plan.over_budget and plan.tool_choice == "none"
    assert nav.stop_reason(plan) == "turn_budget"


def test_stop_reasons_are_distinguishable():
    """三种终态各自不同：用户看到的话术、以及要不要重试，取决于这个。"""
    nav = Navigator(max_rounds=2, max_turn_tokens=100)
    assert nav.stop_reason(nav.plan_round(rounds_done=1, tokens_used=0)) == "stop"
    assert nav.stop_reason(nav.plan_round(rounds_done=2, tokens_used=0)) == "max_rounds"
    assert nav.stop_reason(nav.plan_round(rounds_done=1, tokens_used=999)) == "turn_budget"


def test_narrowing_can_only_shrink_the_tool_surface():
    """技能里写一个本轮没给的工具名，结果是那个工具不可用，而不是把它加进来。"""
    nav = Navigator(max_rounds=5)
    active = ("search_papers", "get_paper")
    block = ToolResultBlock("c", json.dumps({"allowed_tools": ["get_paper", "delete_everything"]}))
    assert nav.narrow_tools([block], active) == ("get_paper",)


def test_empty_intersection_is_ignored_rather_than_muting_the_agent():
    """技能写错名字不该让 Buddy 变成哑巴。"""
    nav = Navigator(max_rounds=5)
    block = ToolResultBlock("c", json.dumps({"allowed_tools": ["拼错了"]}))
    assert nav.narrow_tools([block], ("search_papers",)) is None
    assert nav.narrow_tools([ToolResultBlock("c", "不是 JSON")], ("search_papers",)) is None


# ---- Helm ----


def _ctx() -> ToolContext:
    return ToolContext(project_id=uuid.uuid4(), llm=None, user_id=uuid.uuid4())


async def test_helm_turns_a_crash_into_a_result_instead_of_killing_the_turn():
    @tool(name="_boom", description="炸给你看", input_schema={"type": "object", "properties": {}})
    async def _boom(ctx, args):  # noqa: ANN001
        raise RuntimeError("上游挂了")

    event, block = await Helm(_ctx()).execute(ToolUseBlock("c1", "_boom", {}))
    assert event.ok is False
    assert "RuntimeError" in event.summary
    assert block.is_error and "上游挂了" in block.content


async def test_helm_reports_unknown_tools_and_broken_arguments_differently():
    """两种失败提示不同：一个该换工具，一个该重发这次调用。

    坏参数用**已注册**的工具来测——那才是真实形态：累加器标 __parse_error__ 时，
    工具名通常是好的，坏掉的是流到一半的参数。工具名和参数同时坏掉时先报未知工具，
    因为那条更可执行。
    """

    @tool(name="_known", description="在的", input_schema={"type": "object", "properties": {}})
    async def _known(ctx, args):  # noqa: ANN001
        return {}

    helm = Helm(_ctx())
    unknown, _ = await helm.execute(ToolUseBlock("c1", "_没这个工具", {}))
    broken, _ = await helm.execute(ToolUseBlock("c2", "_known", {"__parse_error__": "..."}))
    assert "未知工具" in unknown.summary
    assert "重新发起" in broken.summary


async def test_helm_times_out_instead_of_hanging_the_turn():
    from app.agents.chat.core import helm as helm_mod

    @tool(name="_slow", description="慢", input_schema={"type": "object", "properties": {}})
    async def _slow(ctx, args):  # noqa: ANN001
        await asyncio.sleep(5)
        return {}

    original = helm_mod.TOOL_TIMEOUT_SECONDS
    helm_mod.TOOL_TIMEOUT_SECONDS = 0.05
    try:
        event, _ = await Helm(_ctx()).execute(ToolUseBlock("c1", "_slow", {}))
    finally:
        helm_mod.TOOL_TIMEOUT_SECONDS = original
    assert event.ok is False and "超过" in event.summary


async def test_helm_keeps_call_order_even_when_they_finish_out_of_order():
    """卡片次序应与模型的意图一致；谁先跑完是实现细节。"""

    @tool(name="_slowish", description="慢一点", input_schema={"type": "object", "properties": {}})
    async def _slowish(ctx, args):  # noqa: ANN001
        await asyncio.sleep(0.05)
        return {"who": "slow"}

    @tool(name="_fast", description="快", input_schema={"type": "object", "properties": {}})
    async def _fast(ctx, args):  # noqa: ANN001
        return {"who": "fast"}

    calls = [ToolUseBlock("c1", "_slowish", {}), ToolUseBlock("c2", "_fast", {})]
    seen = [ev.name async for ev, _ in Helm(_ctx()).run(calls)]
    assert seen == ["_slowish", "_fast"]


# ---- Sextant ----


def test_sextant_says_nothing_when_the_answer_is_supported():
    verdict = Sextant().verify(answer="查到三篇 [1][2][3]。", sources=3, successful_tool_calls=1)
    assert verdict.passed and verdict.notes == ()


def test_sextant_catches_citations_with_nothing_retrieved():
    verdict = Sextant().verify(answer="见 [1]。", sources=0, successful_tool_calls=0)
    assert not verdict.passed
    assert "没有查到任何论文" in verdict.notes[0]


def test_sextant_catches_numbering_that_runs_past_the_evidence():
    """[5] 但只查到 3 篇——模型编号跑偏的经典形态。"""
    verdict = Sextant().verify(answer="见 [1] 与 [5]。", sources=3, successful_tool_calls=2)
    assert not verdict.passed
    assert "[5]" in verdict.notes[0] and "3 篇" in verdict.notes[0]


def test_sextant_catches_claiming_to_have_searched_without_searching():
    verdict = Sextant().verify(answer="我查到库里有相关工作。", sources=0, successful_tool_calls=0)
    assert not verdict.passed
    assert any("没有任何一次成功的检索" in n for n in verdict.notes)


def test_sextant_does_not_fire_on_plain_answers():
    """没有编号、没有声称检索过 → 什么都不说。误报会训练用户忽略这一栏。"""
    assert Sextant().verify(
        answer="这个问题不需要查库，我直接说说思路。", sources=0, successful_tool_calls=0
    ).passed
