"""ACP 的边角（#850）：连接收尾、会话池的锁与取消、模型调用池换代、时间线落库、
交替轮次的上下文、模式/目标/模型跟到 agent 那边。"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

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


def test_target_version_ignores_probe_writes():
    from app.core.llm.router import _acp_target

    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="a",
        slug="a",
        command="cmd",
        args=["x"],
        env_encrypted=None,
        updated_at="2026-10-01",
    )
    first = _acp_target(row)
    row.updated_at = "2026-10-02"  # 探测写回 last_probe
    assert _acp_target(row) == first
    row.args = ["y"]
    assert _acp_target(row).version != first.version


def test_router_drops_providers_of_an_agents_old_config():
    from app.core.llm.router import LLMRouter, ResolvedRoute

    router = LLMRouter()

    def route(target: AcpTarget) -> ResolvedRoute:
        return ResolvedRoute(
            provider_kind="acp", base_url=None, api_key="", model="", temperature=None, acp=target
        )

    router._provider_for(route(_target()), "relevance")
    router._provider_for(route(_target(X="1")), "relevance")
    acps = [p for p in router._providers.values() if isinstance(p, AcpLLMProvider)]
    assert [p.target.env for p in acps] == [(("X", "1"),)]


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


async def test_a_disconnected_turn_is_stored_as_interrupted(client):
    from app.agents.chat.events import DeltaEvent, ToolCallEvent
    from app.api.chat_agent import _stream_turn
    from app.core.db import get_sessionmaker
    from app.services import conversations as store

    await register_and_login(client)
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from app.models.user import User
        from app.services.local_user import LOCAL_USER_EMAIL

        user = (
            await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
        ).scalar_one()
        conv = await store.get_or_create(session, user_id=user.id, scope_kind="global")
        await store.append_message(session, conversation=conv, role="user", text="q")
        await session.commit()
        conv_id, user_id = conv.id, user.id

    async def events():
        yield DeltaEvent("part")
        yield ToolCallEvent(id="c1", name="agent_read", args={})
        await asyncio.sleep(30)

    resp = _stream_turn(
        events(), conv_id=conv_id, user_id=user_id, first_question="q", agent_turn=True
    )
    body = resp.body_iterator
    await body.__anext__()
    await body.__anext__()
    await body.aclose()  # 客户端断了

    from app.models.conversation import ConversationMessage

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == conv_id)
                    .order_by(ConversationMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        assert rows[-1].stop_reason == "interrupted" and rows[-1].status == "interrupted"
        assert [r.kind for r in rows[1:]] == ["normal", "tool_results"]
        history = await store.replay(session, conversation_id=conv_id)
    # 回放里不留没结果的调用
    for msg in history:
        if isinstance(msg.content, list):
            assert not any(isinstance(b, ToolUseBlock) for b in msg.content)


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


async def test_nested_agent_tool_calls_replay_cleanly(client, agent_on):
    from app.core.db import get_sessionmaker
    from app.services import conversations as store

    headers = await _headers(client)
    agent = await _register(client, headers)
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    frames = await _turn(client, headers, conv_id, "nested", backend=agent["id"])
    assert frames[-1][0] == "done" and _said(frames) == "all done"
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    kinds = [(m["role"], m["kind"]) for m in msgs]
    assert kinds == [
        ("user", "normal"),
        ("assistant", "normal"),
        ("user", "tool_results"),
        ("assistant", "normal"),
    ]
    uses = [b["id"] for b in msgs[1]["blocks"] if b["kind"] == "tool_use"]
    results = [b["tool_use_id"] for b in msgs[2]["blocks"]]
    assert uses == ["t1", "t2", "t3"] and sorted(results) == ["t1", "t2", "t3"]
    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=uuid.UUID(conv_id))
    assert len(history) == 4  # 修补无事可做：本来就配好了对


async def test_turns_answered_by_polaris_reach_a_live_agent_session(client, agent_on):
    """agent 答一轮 → Polaris 答一轮 → 再交给 agent：中间那轮它得看得见，更早的不重复。"""
    from app.core.db import get_sessionmaker
    from app.services import conversations as store

    headers = await _headers(client)
    agent = await _register(client, headers)
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    await _turn(client, headers, conv_id, "first agent question", backend=agent["id"])
    async with get_sessionmaker()() as session:
        conv = await store.get_owned(
            session, conversation_id=uuid.UUID(conv_id), user_id=(await _user_id(session))
        )
        assert isinstance(conv.settings.get("acp_seen_seq"), int)
        # Polaris 自己答的一轮（原生循环落库的样子）
        await store.append_message(session, conversation=conv, role="user", text="native question")
        await store.append_message(
            session, conversation=conv, role="assistant", text="native answer"
        )
        await session.commit()
    said = _said(await _turn(client, headers, conv_id, "prompt-text", backend=agent["id"]))
    assert "native question" in said and "native answer" in said
    assert "first agent question" not in said  # 它自己的会话里本来就有
    # 再来一轮：没有新的旁路内容，就不再补
    said = _said(await _turn(client, headers, conv_id, "prompt-text again", backend=agent["id"]))
    assert "native answer" not in said


async def _user_id(session) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.user import User
    from app.services.local_user import LOCAL_USER_EMAIL

    return (
        await session.execute(select(User.id).where(User.email == LOCAL_USER_EMAIL))
    ).scalar_one()


async def test_polaris_turns_on_an_agent_carry_mode_memory_and_model(client, agent_on):
    from app.services.acp import chat as acp_chat

    headers = await _headers(client)
    agent = await _register(client, headers)
    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "agent", "acp_agent_id": agent["id"], "model": "smart"}],
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    await client.post("/api/chat/memories", json={"text": "prefers short answers"}, headers=headers)
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    frames = await _turn(
        client, headers, conv_id, "prompt-text", backend="polaris", mode="goal", goal="ship #850"
    )
    said = _said(frames)
    assert frames[0][1]["model"] == "Fake Agent"
    assert "prefers short answers" in said and "ship #850" in said
    live = acp_chat.live_session(uuid.UUID(conv_id))
    assert live is not None and live.model == "smart"
    # 计划模式：给 agent 的是不提 submit_plan 的那份
    said = _said(
        await _turn(client, headers, conv_id, "prompt-text", backend="polaris", mode="plan")
    )
    assert "计划模式" in said and "submit_plan" not in said


async def test_agents_cannot_serve_capability_stages(client):
    headers = await _headers(client)
    agent = await _register(client, headers)
    for stage in ("embedding", "rerank"):
        resp = await client.put(
            "/api/admin/llm/routes",
            json=[{"stage": stage, "acp_agent_id": agent["id"], "model": ""}],
            headers=headers,
        )
        assert resp.status_code == 400, resp.text


async def test_testing_an_agent_with_an_unknown_model_says_so(client):
    headers = await _headers(client)
    agent = await _register(client, headers)
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"acp_agent_id": agent["id"], "model": "gpt-9"},
        headers=headers,
    )
    body = resp.json()
    assert body["ok"] is False and "gpt-9" in body["error"] and "fast, smart" in body["error"]
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"acp_agent_id": agent["id"], "model": "fast"},
        headers=headers,
    )
    assert resp.json()["ok"] is True
