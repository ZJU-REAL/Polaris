"""ACP 的边角（#850）：连接收尾、会话池的锁与取消、模型调用池换代、时间线落库、
交替轮次的上下文、模式/目标/模型跟到 agent 那边。"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.core.llm import acp as acp_llm
from app.core.llm.acp import AcpLLMProvider, AcpTarget
from app.core.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock
from app.services.acp import connection as acp_connection
from app.services.acp.client import AcpClient, AgentSpec
from app.services.acp.connection import AcpError
from app.services.acp.pool import AcpSessionPool
from tests.conftest import register_and_login

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


def _spec(**env: str) -> AgentSpec:
    return AgentSpec(name="fake", command=sys.executable, args=(FAKE,), env=dict(env))


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # 僵尸也算没了（父进程还没收尸）
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] == "Z"
    except OSError:
        return False


async def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        await asyncio.sleep(0.1)
    return _gone(pid)


@pytest.fixture(autouse=True)
async def _clean_pools():
    yield
    await acp_llm.shutdown_pools()
    from app.services.acp.pool import shutdown_pool

    await shutdown_pool()


# ---------------------------------------------------------------- 连接


async def test_an_oversized_line_kills_the_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_connection, "LINE_LIMIT", 4096)
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        with pytest.raises(AcpError):
            async with asyncio.timeout(10):
                _ = [e async for e in client.prompt(sid, "huge")]
        # 帧边界乱了：连接作废、进程收掉，而不是被当成活的继续派活
        assert not client.alive
        assert await _wait_gone(client._conn._proc.pid)
    finally:
        await client.close()


async def test_eof_marks_the_connection_dead(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        with pytest.raises(AcpError):
            _ = [e async for e in client.prompt(sid, "crash")]
        assert not client.alive
    finally:
        await client.close()


async def test_an_error_with_a_null_id_fails_the_request(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        with pytest.raises(AcpError) as info:
            await client._conn.request(
                "session/set_model", {"sessionId": sid, "modelId": "broken"}, timeout=10
            )
        assert info.value.code == "rpc-error"  # 不是干等到超时
    finally:
        await client.close()


async def test_an_abandoned_wait_leaves_no_queue_getter(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        gen = client.prompt(sid, "slow")
        assert (await gen.__anext__())["type"] == "message"
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        nxt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await nxt
        await gen.aclose()
        await asyncio.sleep(0)
        getters = [
            t
            for t in asyncio.all_tasks()
            if not t.done() and "Queue.get" in getattr(t.get_coro(), "__qualname__", "")
        ]
        assert getters == []
    finally:
        await client.close()


# ---------------------------------------------------------------- 会话池


async def test_a_slow_open_does_not_block_other_conversations(tmp_path):
    pool = AcpSessionPool()
    slow_agent = uuid.uuid4()
    try:
        slow = asyncio.create_task(
            pool.open(
                "a",
                agent_id=slow_agent,
                spec=_spec(FAKE_ACP_SLOW_NEW="3"),
                cwd=str(tmp_path),
            )
        )
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        live_b = await pool.open("b", agent_id=uuid.uuid4(), spec=_spec(), cwd=str(tmp_path))
        await pool.forget_agent(uuid.uuid4())  # 改/删别的 agent 也不该被堵住
        await pool.reap()
        assert time.monotonic() - t0 < 2.5
        assert live_b.client.alive and not slow.done()
        live_a = await slow
        assert live_a.client.alive
    finally:
        await pool.close_all()


async def test_forgetting_an_agent_mid_open_discards_the_new_session(tmp_path):
    pool = AcpSessionPool()
    agent = uuid.uuid4()
    seen: list[AcpClient] = []

    async def factory(client):
        seen.append(client)
        return [], []

    try:
        opening = asyncio.create_task(
            pool.open(
                "k",
                agent_id=agent,
                spec=_spec(FAKE_ACP_SLOW_NEW="1"),
                cwd=str(tmp_path),
                mcp_servers=factory,
            )
        )
        await asyncio.sleep(0.4)
        await pool.forget_agent(agent)
        with pytest.raises(AcpError):
            await opening
        assert pool.get("k") is None
        assert not seen[0].alive
    finally:
        await pool.close_all()


async def test_close_if_leaves_a_newer_session_alone(tmp_path):
    pool = AcpSessionPool()
    agent = uuid.uuid4()
    try:
        old = await pool.open("k", agent_id=agent, spec=_spec(), cwd=str(tmp_path))
        await pool.close("k")
        new = await pool.open("k", agent_id=agent, spec=_spec(), cwd=str(tmp_path))
        await pool.close_if("k", old)
        assert pool.get("k") is new and new.client.alive
        await pool.close_if("k", new)
        assert pool.get("k") is None and not new.client.alive
    finally:
        await pool.close_all()


async def test_cancelled_open_still_kills_the_agent_and_revokes(tmp_path):
    """断线时的取消（anyio 取消域会在每个 await 上反复投递）不能让收尾半途而废。"""
    import anyio

    pool = AcpSessionPool()
    revoked: list[int] = []
    clients: list[AcpClient] = []

    async def factory(client):
        clients.append(client)

        async def revoke() -> None:
            await asyncio.sleep(0.05)
            revoked.append(1)

        return [], [revoke]

    with anyio.CancelScope() as scope:

        async def canceller() -> None:
            await asyncio.sleep(1.0)
            scope.cancel()

        killer = asyncio.create_task(canceller())
        await pool.open(
            "k",
            agent_id=uuid.uuid4(),
            spec=_spec(FAKE_ACP_SLOW_NEW="30", FAKE_ACP_IGNORE_TERM="1"),
            cwd=str(tmp_path),
            mcp_servers=factory,
        )
    await killer
    assert scope.cancelled_caught
    pid = clients[0]._conn._proc.pid
    assert await _wait_gone(pid), "agent process survived a cancelled open"
    for _ in range(50):
        if revoked:
            break
        await asyncio.sleep(0.1)
    assert revoked == [1]
    await pool.close_all()


async def test_a_cancelled_session_close_still_runs_its_hooks(tmp_path):
    pool = AcpSessionPool()
    revoked: list[str] = []

    async def factory(client):
        async def slow_hook() -> None:
            await asyncio.sleep(0.5)
            revoked.append("slow")

        async def revoke() -> None:
            revoked.append("revoke")

        return [], [slow_hook, revoke]

    live = await pool.open(
        "k",
        agent_id=uuid.uuid4(),
        spec=_spec(),
        cwd=str(tmp_path),
        mcp_servers=factory,
    )
    pid = live.client._conn._proc.pid
    closing = asyncio.create_task(pool.close("k"))
    await asyncio.sleep(0.3)  # 进程已关，正卡在第一个钩子上
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert await _wait_gone(pid)
    for _ in range(50):
        if len(revoked) == 2:
            break
        await asyncio.sleep(0.1)
    # suppress(Exception) 接不住 CancelledError：以前第二个钩子（吊销令牌）永远跑不到
    assert revoked == ["slow", "revoke"]


# ---------------------------------------------------------------- 模型调用的进程池


def _target(**env: str) -> AcpTarget:
    return AcpTarget(
        agent_id="fake-1",
        name="Fake",
        command=sys.executable,
        args=(FAKE,),
        env=tuple(sorted(env.items())),
    )


async def test_a_replaced_pool_does_not_keep_in_flight_workers():
    pool = acp_llm.pool_for(_target())
    async with pool.worker() as w:
        # 答题途中配置变了：换新池子，旧池子关掉
        newer = acp_llm.pool_for(_target(X="1"))
        assert newer is not pool
        await asyncio.sleep(0)
    # 答完的进程没有回到那个已经没人用的旧池子里，而是被关掉
    assert pool._idle == [] and not w.client.alive
    assert newer._sem is pool._sem  # 新旧两代共用同一个并发上限


async def test_a_rename_does_not_churn_the_pool():
    pool = acp_llm.pool_for(_target())
    renamed = AcpTarget(
        agent_id="fake-1", name="Renamed", command=sys.executable, args=(FAKE,), env=()
    )
    assert acp_llm.pool_for(renamed) is pool


async def test_an_unknown_model_is_logged_and_the_default_answers(caplog):
    provider = AcpLLMProvider(_target())
    with caplog.at_level("WARNING", logger="polaris.acp"):
        result = await provider.complete([Message("user", "hi")], model="no-such-model")
    assert result.content == "llm[default]: hi"
    assert "no-such-model" in caplog.text and "fast" in caplog.text


async def test_strict_providers_refuse_an_unknown_model():
    provider = AcpLLMProvider(_target(), strict_model=True)
    with pytest.raises(AcpError) as info:
        await provider.complete([Message("user", "hi")], model="no-such-model")
    assert "fast, smart" in str(info.value)


# ---------------------------------------------------------------- 时间线落库与回放


def _events_nested():
    from app.agents.chat.events import DeltaEvent, ToolCallEvent, ToolResultEvent

    def call(i: str) -> ToolCallEvent:
        return ToolCallEvent(id=i, name="agent_read", args={"title": i, "input": ""})

    def result(i: str) -> ToolResultEvent:
        return ToolResultEvent(
            id=i, name="agent_read", ok=True, summary=i, preview="", duration_ms=1
        )

    # t1 套着 t2；t3 开了头没有下文；t1 最后才结束
    return [
        DeltaEvent("looking"),
        call("t1"),
        call("t2"),
        result("t2"),
        call("t3"),
        result("t1"),
        DeltaEvent("done"),
    ]


def _assert_paired(parts):
    for index, (role, blocks) in enumerate(parts):
        ids = [b.id for b in blocks if isinstance(b, ToolUseBlock)]
        if role == "assistant" and ids:
            role2, results = parts[index + 1]
            assert role2 == "tool_results"
            assert sorted(b.tool_use_id for b in results) == sorted(ids)


def test_agent_timeline_pairs_every_call_with_a_result():
    from app.api.chat_agent import _TurnTimeline

    timeline = _TurnTimeline(grouped=True)
    for ev in _events_nested():
        timeline.feed(ev)
    parts = timeline.messages()
    assert [r for r, _ in parts] == ["assistant", "tool_results", "assistant"]
    assert [type(b).__name__ for b in parts[0][1]] == [
        "TextBlock",
        "ToolUseBlock",
        "ToolUseBlock",
        "ToolUseBlock",
    ]
    _assert_paired(parts)
    missing = next(b for b in parts[1][1] if b.tool_use_id == "t3")
    assert missing.is_error and "No result reported" in missing.content
    assert parts[2][1][0].text == "done"
    # 照事件顺序切（旧做法）会切出没有结果的 tool_use；现在的默认切法也会补齐
    ungrouped = _TurnTimeline()
    for ev in _events_nested():
        ungrouped.feed(ev)
    _assert_paired(ungrouped.messages())


def test_replay_repair_drops_unpaired_tool_blocks():
    from app.services.conversations import repair_tool_pairs

    msgs = [
        Message("user", "q"),
        Message(
            "assistant", [TextBlock("a"), ToolUseBlock("x", "t", {}), ToolUseBlock("y", "t", {})]
        ),
        Message("user", [ToolResultBlock("x", "ok"), ToolResultBlock("z", "orphan")]),
        Message("assistant", [ToolUseBlock("w", "t", {})]),  # 结果那条被中断滤掉了
        Message("user", "next"),
    ]
    out = repair_tool_pairs(msgs)
    assert [m.role for m in out] == ["user", "assistant", "user", "user"]
    assert [b.id for b in out[1].content if isinstance(b, ToolUseBlock)] == ["x"]
    assert [b.tool_use_id for b in out[2].content] == ["x"]


# ---------------------------------------------------------------- 经助手接口


@pytest.fixture
def agent_on(monkeypatch, tmp_path):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    monkeypatch.setenv("POLARIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


async def _headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {await register_and_login(client)}"}


async def _register(client, headers, **extra) -> dict:
    body = {"slug": "fake", "name": "Fake Agent", "command": sys.executable, "args": [FAKE]}
    resp = await client.post("/api/acp-agents", json={**body, **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _turn(client, headers, conv_id: str, question: str, **extra) -> list[tuple[str, dict]]:
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": question, **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    frames = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    return frames


def _said(frames) -> str:
    return "".join(d["text"] for n, d in frames if n == "delta")


async def _user_id(session) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.user import User
    from app.services.local_user import LOCAL_USER_EMAIL

    return (
        await session.execute(select(User.id).where(User.email == LOCAL_USER_EMAIL))
    ).scalar_one()
