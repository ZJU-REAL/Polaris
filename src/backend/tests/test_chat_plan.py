"""任务计划：update_plan 工具 + 循环补发的 plan 帧。

计划的价值在于**可寻址**：正文里写「我打算先……再……」漂在文字里，谁也更新不了、
前端也画不出来。所以这里钉两件事：计划本身不合法要报错（不要画空白进度条），
以及界面看到的计划与模型看到的是同一份。
"""

import json
import os
import uuid

import pytest

from app.agents.chat.events import PlanEvent, ToolResultEvent
from app.agents.chat.loop import _plan_from
from app.core.llm.base import ToolResultBlock
from app.tools.plan import MAX_STEPS, PlanError, normalize
from tests.conftest import register_and_login


def test_normalize_keeps_order_and_defaults_status():
    steps = normalize([{"title": "查文献"}, {"title": "读全文", "status": "running"}])
    assert [s["title"] for s in steps] == ["查文献", "读全文"]
    assert steps[0]["status"] == "pending"  # 没给就是没开始
    assert steps[1]["status"] == "running"


def test_normalize_rejects_what_would_draw_an_empty_progress_bar():
    """空计划和没标题的步骤必须报错——画出来的空白进度条比不画更糟。"""
    with pytest.raises(PlanError):
        normalize([])
    with pytest.raises(PlanError):
        normalize("不是列表")
    with pytest.raises(PlanError):
        normalize([{"title": "  "}])
    with pytest.raises(PlanError):
        normalize([{"title": f"第{i}步"} for i in range(MAX_STEPS + 1)])


def test_only_one_step_can_be_running():
    """并行推进的计划没人看得懂。"""
    with pytest.raises(PlanError):
        normalize([{"title": "a", "status": "running"}, {"title": "b", "status": "running"}])


def test_unknown_status_falls_back_instead_of_failing():
    """状态拼错不该让整条计划作废——宽进严出。"""
    assert normalize([{"title": "a", "status": "在做"}])[0]["status"] == "pending"


def test_plan_frame_is_read_from_what_the_model_got():
    """plan 帧读的是回喂给模型的那份文本，不是另算一份。

    否则用户按着进度条问「第二步呢」，模型一脸茫然。
    """
    steps = [{"title": "查", "status": "done"}, {"title": "读", "status": "running"}]
    block = ToolResultBlock("call-1", json.dumps({"steps": steps}, ensure_ascii=False))
    ev = ToolResultEvent(
        id="call-1", name="update_plan", ok=True, summary="", preview="", duration_ms=1
    )
    assert _plan_from(ev, block) == tuple(steps)


def test_plan_frame_is_not_emitted_for_other_or_failed_calls():
    block = ToolResultBlock("c", json.dumps({"steps": [{"title": "x", "status": "pending"}]}))
    other = ToolResultEvent(
        id="c", name="search_papers", ok=True, summary="", preview="", duration_ms=1
    )
    failed = ToolResultEvent(
        id="c", name="update_plan", ok=False, summary="", preview="", duration_ms=1
    )
    broken = ToolResultEvent(
        id="c", name="update_plan", ok=True, summary="", preview="", duration_ms=1
    )
    assert _plan_from(other, block) is None
    assert _plan_from(failed, block) is None
    assert _plan_from(broken, ToolResultBlock("c", "{不是 JSON")) is None
    assert _plan_from(broken, None) is None


def test_the_plan_tool_stays_out_of_the_mcp_listing():
    """外部 MCP 客户端没有对话、也没有能画进度的界面，给它一个回声工具只是噪音。"""
    from app.mcp.dispatch import tool_definitions

    assert "update_plan" not in {t["name"] for t in tool_definitions()}


async def test_the_tool_normalizes_through_the_registry():
    from app.tools.context import ToolContext
    from app.tools.registry import run_tool

    ctx = ToolContext(project_id=uuid.uuid4(), llm=None, user_id=uuid.uuid4())
    out = await run_tool(ctx, "update_plan", {"steps": [{"title": "只有一步"}]})
    assert out == {"steps": [{"title": "只有一步", "status": "pending"}]}


@pytest.fixture
def agent_on(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


async def test_the_loop_emits_a_plan_event_after_a_successful_update(agent_on):
    """整条链路：模型调 update_plan → 循环补一帧 PlanEvent。"""
    from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
    from app.core.llm.fake import FakeProvider
    from app.core.llm.router import LLMRouter
    from app.tools.context import ToolContext

    class _ScriptedRouter(LLMRouter):
        """只替换 stream_events：不碰数据库配置，也就不会去写 llm_usage。"""

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

    provider = FakeProvider()
    provider.script(
        [
            [("update_plan", {"steps": [{"title": "先查", "status": "running"}]})],
            "查完了，结论是……",
        ]
    )
    ctx = ToolContext(project_id=uuid.uuid4(), llm=LLMRouter(), user_id=uuid.uuid4())
    loop = ChatAgentLoop(llm=_ScriptedRouter(provider), tool_ctx=ctx)
    events = [
        ev
        async for ev in loop.run(
            ChatTurnRequest(
                conversation_id=uuid.uuid4(),
                question="帮我做件多步的事",
                tool_names=("update_plan",),
            )
        )
    ]
    plans = [ev for ev in events if isinstance(ev, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].steps == ({"title": "先查", "status": "running"},)


async def test_the_plan_frame_reaches_the_client(client, agent_on):
    """SSE 上真的有一帧 event: plan，内容与工具结果一致。

    用 POLARIS_FAKE_TOOL 标记驱动**真的 update_plan**，走完整条端点路径——
    循环里补发那一帧的耦合只有端到端才验得出来。
    """
    token = await register_and_login(client, email="plan-sse@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    # 助手在用户没有课题时不带任何工具（诚实：不会假装查过），所以先建一个
    await client.post(
        "/api/projects", json={"name": "plan-proj", "statement": "agent"}, headers=headers
    )
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    marker = (
        'POLARIS_FAKE_TOOL:update_plan:'
        '{"steps": [{"title": "\u5148\u67e5", "status": "running"}, {"title": "\u518d\u8bfb"}]}'
    )
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"多步任务\n{marker}"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        body = (await stream.aread()).decode()

    frames = [
        json.loads(line[len("data: ") :])
        for block in body.strip().split("\n\n")
        if "event: plan" in block
        for line in block.split("\n")
        if line.startswith("data: ")
    ]
    assert frames, f"没有 plan 帧：{body[:400]}"
    assert frames[0]["steps"] == [
        {"title": "先查", "status": "running"},
        {"title": "再读", "status": "pending"},
    ]
