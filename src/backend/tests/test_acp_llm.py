"""外部 agent 当模型用（#840）：provider、进程池、路由接管、设置接口、助手。"""

import asyncio
import json
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
from tests.conftest import register_and_login

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


async def test_an_agent_takes_over_when_no_model_api_is_configured(client, no_fake_fallback):
    from app.core.llm.router import LLMNotConfiguredError, get_llm_router

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    router = get_llm_router()
    with pytest.raises(LLMNotConfiguredError):
        await router.resolve("relevance")
    onboarding = (await client.get("/api/onboarding", headers=headers)).json()
    assert next(i for i in onboarding["items"] if i["id"] == "model")["done"] is False

    agent = await _register(client, headers)
    provider, route = await router.resolve("relevance")
    assert route.provider_kind == "acp" and route.acp.agent_id == agent["id"]
    assert isinstance(provider, AcpLLMProvider)
    # 能力型环节不接管：agent 产不出向量，照旧走降级
    with pytest.raises(NotImplementedError):
        await router.resolve("embedding")

    result = await router.complete("relevance", [Message("user", "rate it")])
    assert result.content == "llm[default]: rate it"
    onboarding = (await client.get("/api/onboarding", headers=headers)).json()
    assert next(i for i in onboarding["items"] if i["id"] == "model")["done"] is True

    # 停用后不再接管
    await client.patch(f"/api/acp-agents/{agent['id']}", json={"enabled": False}, headers=headers)
    with pytest.raises(LLMNotConfiguredError):
        await router.resolve("relevance")


async def test_routes_can_target_an_agent(client):
    from app.core.llm.router import get_llm_router

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    agent = await _register(client, headers)
    bad = [{"stage": "default", "model": "x"}]  # 两个都没给
    assert (await client.put("/api/admin/llm/routes", json=bad, headers=headers)).status_code == 422
    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "relevance", "acp_agent_id": agent["id"], "model": "fast"}],
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["acp_agent_id"] == agent["id"] and resp.json()[0]["provider_id"] is None
    result = await get_llm_router().complete("relevance", [Message("user", "hi")])
    assert result.content == "llm[fast]: hi"

    # 测试连接：真让 agent 答一句
    test = await client.post(
        "/api/admin/llm/test-model", json={"acp_agent_id": agent["id"]}, headers=headers
    )
    assert test.status_code == 200 and test.json()["ok"] is True, test.text

    # 删掉 agent：指向它的路由跟着删（外键级联），不留一行指向不存在的东西
    await client.delete(f"/api/acp-agents/{agent['id']}", headers=headers)
    routes = (await client.get("/api/admin/llm/routes", headers=headers)).json()
    assert routes == []


async def test_polaris_assistant_runs_on_the_agent_when_it_takes_over(
    client, no_fake_fallback, monkeypatch, tmp_path
):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    monkeypatch.setenv("POLARIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    await _register(client, headers)
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "hello there", "backend": "polaris"},
        headers=headers,
    )
    assert resp.status_code == 200
    frames = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    assert frames[0][0] == "meta" and frames[0][1]["model"] == "Fake Agent"
    assert frames[-1][0] == "done"
    text = "".join(d["text"] for n, d in frames if n == "delta")
    # 走的是 agent 会话（默认脚本：读 notes、回显问题），不是一次性补全
    assert text == "echo: hello there"
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()
