"""外部 agent 当模型用（#840）：provider、进程池、路由接管、设置接口、助手。"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.core.llm import acp as acp_llm
from app.core.llm.acp import AcpLLMProvider, AcpTarget, render_prompt
from app.core.llm.base import (
    ImageBlock,
    Message,
    StreamDone,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolsUnsupportedError,
)

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


def _target(**env: str) -> AcpTarget:
    return AcpTarget(
        agent_id="fake-1",
        name="Fake",
        command=sys.executable,
        args=(FAKE,),
        env=tuple(sorted(env.items())),
    )


@pytest.fixture(autouse=True)
async def _clean_pools():
    yield
    await acp_llm.shutdown_pools()
    from app.services.acp.pool import shutdown_pool

    await shutdown_pool()


# ---------------------------------------------------------------- 提示词


def test_render_prompt_flattens_system_and_history():
    one = render_prompt([Message("system", "Be brief."), Message("user", "Score this paper.")])
    assert one.startswith("Instructions:\nBe brief.")
    assert "Score this paper." in one and "[User]" not in one  # 单轮不加角色标签
    assert one.rstrip().endswith(
        "exactly (for example, reply with only the JSON when JSON is asked for)."
    )

    multi = render_prompt(
        [Message("user", "Hi"), Message("assistant", "Hello"), Message("user", "And now?")]
    )
    assert "answer the last User message" in multi
    assert (
        multi.index("[User]\nHi")
        < multi.index("[Assistant]\nHello")
        < multi.index("[User]\nAnd now?")
    )


# ---------------------------------------------------------------- provider


async def test_complete_returns_the_agents_answer():
    provider = AcpLLMProvider(_target())
    result = await provider.complete([Message("user", "Give me a title.")], model="")
    assert result.content == "llm[default]: Give me a title."
    assert result.finish_reason == "stop"
    assert result.usage == {}  # 用量由路由器按字数估算


async def test_route_model_is_applied_when_the_agent_offers_it():
    provider = AcpLLMProvider(_target())
    assert (await provider.complete([Message("user", "x")], model="smart")).content.startswith(
        "llm[smart]"
    )
    # agent 没提供的模型名不硬塞：照常用它的默认模型
    assert (await provider.complete([Message("user", "x")], model="gpt-9")).content.startswith(
        "llm[default]"
    )


async def test_streams_text_and_thinking():
    provider = AcpLLMProvider(_target())
    events = [e async for e in provider.stream_events([Message("user", "abc")], model="")]
    assert isinstance(events[0], ThinkingDelta) and events[0].text == "considering"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "llm[default]: abc"
    assert isinstance(events[-1], StreamDone)
    chunks = [c async for c in provider.stream([Message("user", "abc")], model="")]
    assert "".join(chunks) == "llm[default]: abc"


async def test_images_go_through_only_when_the_agent_can_see_them():
    msg = Message("user", [TextBlock("Describe."), ImageBlock(b"\x89PNG", "image/png")])
    seeing = await AcpLLMProvider(_target(FAKE_ACP_IMAGE="1")).complete(
        [msg], model="", images=[b"\x89PNG"]
    )
    assert seeing.content.endswith("[images=2]")
    blind = await AcpLLMProvider(_target()).complete([msg], model="")
    assert "[images=" not in blind.content
    assert "cannot view images" in blind.content  # 说明被拼进了提示词


async def test_tools_are_refused_so_callers_degrade():
    provider = AcpLLMProvider(_target())
    with pytest.raises(ToolsUnsupportedError):
        await provider.complete([Message("user", "x")], model="", tools=[{"name": "t"}])


async def test_pool_reuses_processes_and_caps_concurrency(monkeypatch):
    monkeypatch.setattr(acp_llm, "DEFAULT_CONCURRENCY", 2)
    await acp_llm.shutdown_pools()
    provider = AcpLLMProvider(_target())
    results = await asyncio.gather(
        *(provider.complete([Message("user", f"q{i}")], model="") for i in range(5))
    )
    assert [r.content for r in results] == [f"llm[default]: q{i}" for i in range(5)]
    pool = acp_llm.pool_for(_target())
    assert len(pool._idle) <= 2  # 至多两个常驻进程
    pids = {w.client._conn._proc.pid for w in pool._idle}
    await provider.complete([Message("user", "again")], model="")
    assert {w.client._conn._proc.pid for w in pool._idle} <= pids  # 复用，不新起


async def test_a_missing_command_fails_cleanly():
    provider = AcpLLMProvider(AcpTarget(agent_id="x", name="x", command="no-such-agent-xyz"))
    from app.services.acp.connection import AcpError

    with pytest.raises(AcpError):
        await provider.complete([Message("user", "x")], model="")


# ---------------------------------------------------------------- 路由接管


async def _register(client, headers, **extra) -> dict:
    body = {
        "slug": extra.pop("slug", "fake"),
        "name": "Fake Agent",
        "command": sys.executable,
        "args": [FAKE],
        **extra,
    }
    resp = await client.post("/api/acp-agents", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def no_fake_fallback(monkeypatch):
    """关掉测试套件的 fake 兜底，才看得出「没配模型」时到底谁来接。"""
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_LLM_FAKE_FALLBACK", "0")
    get_settings.cache_clear()
    yield
    os.environ["POLARIS_LLM_FAKE_FALLBACK"] = "1"
    get_settings.cache_clear()
