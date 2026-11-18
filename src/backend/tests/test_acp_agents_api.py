"""本机智能体：管理面（列表/模板/添加/修改/删除/检查连接）+ 助手对话交给智能体（#836）。

Polaris 自己的模型调用不经过智能体：只有用户为一场对话明确选了某个智能体，那场对话
才由它来答；没选过的对话（含老对话）照旧由 Polaris 自己的循环答。
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from app.core.llm.base import ToolUseBlock
from tests.conftest import register_and_login

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


@pytest.fixture
def agent_on(monkeypatch, tmp_path):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    monkeypatch.setenv("POLARIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean_pool():
    yield
    from app.services.acp.pool import shutdown_pool

    await shutdown_pool()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    return events


def _said(frames: list[tuple[str, dict]]) -> str:
    return "".join(d["text"] for n, d in frames if n == "delta")


async def _owner(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {await register_and_login(client)}"}


async def _register_fake(client, headers, **extra) -> dict:
    body = {
        "slug": "fake",
        "name": "Fake Agent",
        "command": sys.executable,
        "args": [FAKE],
        **extra,
    }
    resp = await client.post("/api/acp-agents", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _new_conv(client, headers) -> str:
    return (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]


async def _turn(client, headers, conv_id: str, question: str, **extra) -> list[tuple[str, dict]]:
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": question, **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return _parse_sse(resp.text)


# ---------------------------------------------------------------- 管理面


async def test_templates_list_known_agents_including_pi(client):
    headers = await _owner(client)
    resp = await client.get("/api/acp-agents/templates", headers=headers)
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()}
    assert {"claude-code", "codex", "pi", "gemini", "qwen", "opencode", "kimi"} <= set(by_id)

    claude = by_id["claude-code"]
    assert claude["command"] == "claude-agent-acp" and "installed" in claude
    # 适配器自带 CLI：装没装只看适配器
    assert claude["detect"] == ["claude-agent-acp"]
    # Polaris 自己的 key 不交给 Claude Code：它该用用户自己的登录
    assert not any(n.startswith("ANTHROPIC") for n in claude["env_passthrough"])

    pi = by_id["pi"]
    assert pi["command"] == "pi-acp" and pi["args"] == []
    assert pi["detect"] == ["pi-acp", "pi"]
    assert "pi-acp" in pi["install"] and "@earendil-works/pi-coding-agent" in pi["install"]
    assert pi["login"] and pi["homepage"].startswith("https://")
    # pi 动手前不问：权限策略对它不起作用，界面要说清
    assert pi["permission_prompts"] is False
    assert by_id["codex"]["permission_prompts"] is True

    assert by_id["gemini"]["args"] == ["--acp"]
    assert by_id["opencode"]["args"] == ["acp"]


async def test_crud_probe_and_env_never_returned(client):
    headers = await _owner(client)
    row = await _register_fake(client, headers, env={"FAKE_AGENT_TOKEN": "secret-value"})
    assert row["permission_policy"] == "ask"  # 新登记默认每次询问
    assert row["env_keys"] == ["FAKE_AGENT_TOKEN"] and "secret-value" not in json.dumps(row)
    assert row["command_found"] is True
    assert row["error_kind"] is None and row["permission_prompts"] is True

    listed = (await client.get("/api/acp-agents", headers=headers)).json()
    assert [a["id"] for a in listed] == [row["id"]]

    probed = (await client.post(f"/api/acp-agents/{row['id']}/probe", headers=headers)).json()
    assert probed["last_error"] is None and probed["error_kind"] is None
    assert probed["last_probe"]["name"] == "fake-agent"
    assert probed["last_probed_at"]

    resp = await client.patch(
        f"/api/acp-agents/{row['id']}", json={"permission_policy": "nonsense"}, headers=headers
    )
    assert resp.status_code == 400
    resp = await client.patch(
        f"/api/acp-agents/{row['id']}",
        json={"permission_policy": "read_only", "name": "Renamed"},
        headers=headers,
    )
    assert resp.json()["permission_policy"] == "read_only" and resp.json()["name"] == "Renamed"
    # 名字和策略变了不影响上次检查；拉起方式变了才作废
    assert resp.json()["last_probe"] is not None
    resp = await client.patch(
        f"/api/acp-agents/{row['id']}", json={"args": ["/no/such/script.py"]}, headers=headers
    )
    assert resp.json()["last_probe"] is None

    assert (await client.delete(f"/api/acp-agents/{row['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/acp-agents", headers=headers)).json() == []


async def test_probe_failure_is_data_with_an_install_hint(client):
    headers = await _owner(client)
    resp = await client.post(
        "/api/acp-agents",
        json={"slug": "pi", "template": "pi", "command": "pi-acp-not-installed-xyz"},
        headers=headers,
    )
    row = resp.json()
    assert row["command_found"] is False and row["permission_prompts"] is False
    probed = (await client.post(f"/api/acp-agents/{row['id']}/probe", headers=headers)).json()
    assert probed["error_kind"] == "not_installed"
    assert "npm install -g @earendil-works/pi-coding-agent pi-acp" in probed["last_error"]


async def test_probe_reports_a_missing_login(client):
    """握手不查登录的智能体，到开会话才说「先登录」：检查连接要走到开会话这一步。"""
    headers = await _owner(client)
    row = await _register_fake(client, headers, env={"FAKE_ACP_AUTH_REQUIRED": "1"})
    probed = (await client.post(f"/api/acp-agents/{row['id']}/probe", headers=headers)).json()
    assert probed["last_probe"] is None
    assert probed["error_kind"] == "needs_login"
    assert "Authentication required" in probed["last_error"]


async def test_template_defaults_fill_command_and_args(client):
    headers = await _owner(client)
    resp = await client.post(
        "/api/acp-agents", json={"slug": "gem", "template": "gemini"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["command"] == "gemini" and row["args"] == ["--acp"] and row["name"] == "Gemini CLI"
    resp = await client.post(
        "/api/acp-agents", json={"slug": "gem", "template": "gemini"}, headers=headers
    )
    assert resp.status_code == 409
    resp = await client.post(
        "/api/acp-agents", json={"slug": "x", "template": "nope"}, headers=headers
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/api/acp-agents", json={"slug": "y", "command": "a", "env": {"1BAD": "v"}}, headers=headers
    )
    assert resp.status_code == 400


def test_template_env_passthrough_reaches_only_its_agent(monkeypatch):
    """凭据变量按模板放行：codex 拿得到 OPENAI_API_KEY，自定义命令拿不到；谁都拿不到
    Polaris 自己的 key。"""
    from app.models.acp_agent import AcpAgent
    from app.services.acp.client import inherited_env
    from app.services.acp.registry import spec_from_row

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "polaris-own")
    codex = spec_from_row(AcpAgent(slug="c", template="codex", command="codex-acp", args=[]))
    custom = spec_from_row(AcpAgent(slug="x", template="custom", command="x", args=[]))
    assert inherited_env(extra=codex.passthrough).get("OPENAI_API_KEY") == "sk-test"
    assert "OPENAI_API_KEY" not in inherited_env(extra=custom.passthrough)
    claude = spec_from_row(AcpAgent(slug="cc", template="claude-code", command="c", args=[]))
    for spec in (codex, custom, claude):
        assert "ANTHROPIC_API_KEY" not in inherited_env(extra=spec.passthrough)


# ---------------------------------------------------------------- 助手对话


async def test_assistant_turn_runs_on_the_agent(client, agent_on, tmp_path):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)

    backends = (await client.get("/api/chat/backends", headers=headers)).json()
    assert [b["id"] for b in backends] == ["polaris", agent["id"]]
    assert backends[1]["kind"] == "acp" and backends[1]["name"] == "Fake Agent"

    conv_id = await _new_conv(client, headers)
    events = await _turn(client, headers, conv_id, "what do the notes say", backend=agent["id"])
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert events[0][1]["model"] == "Fake Agent"
    assert {"thinking", "plan", "tool_call", "tool_result", "delta"} <= set(names)
    call = next(d for e, d in events if e == "tool_call")
    assert call["name"] == "agent_read" and call["args"]["title"] == "Read notes.txt"
    assert _said(events) == "echo: what do the notes say"

    # 落库：整条时间线在，而且会话记住了智能体那边的会话号
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    assert msgs[-1]["role"] == "assistant" and "echo:" in msgs[-1]["text"]
    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert next(c for c in listed if c["id"] == conv_id)["backend"] == agent["id"]

    from app.core.db import get_sessionmaker
    from app.models.conversation import Conversation

    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, uuid.UUID(conv_id))
        assert conv is not None
        assert conv.settings["backend"] == agent["id"]
        assert conv.settings["acp_agent_id"] == agent["id"]
        assert conv.settings["acp_session_id"].startswith("sess-")
        assert isinstance(conv.settings["acp_seen_seq"], int)
    # 工作目录按对话隔离，在 data_dir 下
    assert any((tmp_path / "acp-workspaces").rglob(conv_id))

    # 第二轮不传 backend：沿用会话上存的那个
    assert _said(await _turn(client, headers, conv_id, "again")) == "echo: again"


async def test_old_conversations_still_open_and_answer_on_polaris(client, agent_on):
    """没选过回答方的对话（升级前的老对话）照旧由 Polaris 答；登记了智能体也不接管。"""
    headers = await _owner(client)
    await _register_fake(client, headers)
    conv_id = await _new_conv(client, headers)
    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert next(c for c in listed if c["id"] == conv_id)["backend"] is None

    events = await _turn(client, headers, conv_id, "你好")
    assert events[-1][0] == "done"
    assert events[0][1]["model"] != "Fake Agent"
    assert not _said(events).startswith("echo:")
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    assert [m["role"] for m in msgs][:2] == ["user", "assistant"]


async def test_switching_back_to_polaris_replays_the_agent_turn(client, agent_on):
    """智能体答过一轮（带工具调用）之后切回 Polaris：回放配好对，原生循环照常答。"""
    from app.core.db import get_sessionmaker
    from app.services import conversations as store

    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    conv_id = await _new_conv(client, headers)
    await _turn(client, headers, conv_id, "nested", backend=agent["id"])
    events = await _turn(client, headers, conv_id, "继续", backend="polaris")
    assert events[-1][0] == "done" and events[0][1]["model"] != "Fake Agent"
    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=uuid.UUID(conv_id))
    uses = [
        b.id
        for m in history
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert uses == ["t1", "t2", "t3"]
    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert next(c for c in listed if c["id"] == conv_id)["backend"] == "polaris"


async def test_a_removed_agent_is_a_conflict_not_a_crash(client, agent_on):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    conv_id = await _new_conv(client, headers)
    await _turn(client, headers, conv_id, "hello", backend=agent["id"])
    await client.delete(f"/api/acp-agents/{agent['id']}", headers=headers)
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn", json={"question": "again"}, headers=headers
    )
    assert resp.status_code == 409 and resp.json()["detail"] == "ACP_AGENT_NOT_AVAILABLE"
    # 对话本身还在，消息还能读
    msgs = await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)
    assert msgs.status_code == 200 and len(msgs.json()) >= 2


async def test_a_missing_command_is_reported_as_not_installed(client, agent_on):
    headers = await _owner(client)
    resp = await client.post(
        "/api/acp-agents",
        json={"slug": "gone", "template": "codex", "command": "codex-acp-not-installed-xyz"},
        headers=headers,
    )
    agent = resp.json()
    conv_id = await _new_conv(client, headers)
    events = await _turn(client, headers, conv_id, "hi", backend=agent["id"])
    err = next(d for n, d in events if n == "error")
    assert err["code"] == "ACP_AGENT_NOT_INSTALLED"


async def test_a_missing_login_is_reported_as_such(client, agent_on):
    headers = await _owner(client)
    agent = await _register_fake(client, headers, env={"FAKE_ACP_AUTH_REQUIRED": "1"})
    conv_id = await _new_conv(client, headers)
    events = await _turn(client, headers, conv_id, "hi", backend=agent["id"])
    err = next(d for n, d in events if n == "error")
    assert err["code"] == "ACP_AGENT_NEEDS_LOGIN"


async def test_a_crash_mid_turn_is_reported_as_a_crash(client, agent_on):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    conv_id = await _new_conv(client, headers)
    events = await _turn(client, headers, conv_id, "crash", backend=agent["id"])
    err = next(d for n, d in events if n == "error")
    assert err["code"] == "ACP_AGENT_CRASHED"
    # 下一轮重新拉起，照常答
    assert _said(await _turn(client, headers, conv_id, "hello")) == "echo: hello"


async def test_new_process_gets_earlier_turns_as_context(client, agent_on):
    """进程被回收、智能体又不支持续会话时，把之前的对话摘要补进提示词。"""
    headers = await _owner(client)
    agent = await _register_fake(client, headers, env={"FAKE_ACP_NO_LOAD": "1"})
    conv_id = await _new_conv(client, headers)
    await _turn(client, headers, conv_id, "first question", backend=agent["id"])
    from app.services.acp.pool import get_pool

    await get_pool().close(conv_id)  # 模拟空闲超时被回收
    said = _said(await _turn(client, headers, conv_id, "prompt-text"))
    assert "first question" in said


async def test_turns_answered_by_polaris_reach_a_live_agent_session(client, agent_on):
    """智能体答一轮 → Polaris 答一轮 → 再交给智能体：中间那轮它得看得见，更早的不重复。"""
    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    conv_id = await _new_conv(client, headers)
    await _turn(client, headers, conv_id, "first agent question", backend=agent["id"])
    await _turn(client, headers, conv_id, "native question", backend="polaris")
    said = _said(await _turn(client, headers, conv_id, "prompt-text", backend=agent["id"]))
    assert "native question" in said
    assert "first agent question" not in said  # 它自己的会话里本来就有
    said = _said(await _turn(client, headers, conv_id, "prompt-text again"))
    assert "native question" not in said


async def test_permission_round_trip_over_the_api(client, agent_on):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    assert agent["permission_policy"] == "ask"
    conv_id = await _new_conv(client, headers)

    # 测试用的 ASGI 传输会把流式响应攒到结束才交出来，所以不能边读边答：
    # 这一轮放到后台跑，从活会话上找到正在等的请求号，经接口回答。
    turn = asyncio.create_task(_turn(client, headers, conv_id, "write", backend=agent["id"]))
    from app.services.acp import chat as acp_chat

    rid = None
    for _ in range(300):
        live = acp_chat.live_session(uuid.UUID(conv_id))
        pending = live.client.pending_asks(live.session_id) if live else []
        if pending:
            rid = pending[0]
            break
        await asyncio.sleep(0.02)
    assert rid, "the agent never asked"
    url = f"/api/chat/conversations/{conv_id}/permissions/{rid}"
    other = await _new_conv(client, headers)
    r = await client.post(
        f"/api/chat/conversations/{other}/permissions/{rid}",
        json={"option_id": "yes"},
        headers=headers,
    )
    assert r.status_code == 404  # 别的对话答不了这个请求
    r = await client.post(url, json={"option_id": "nope"}, headers=headers)
    assert r.status_code == 400, r.text
    r = await client.post(url, json={"option_id": "yes"}, headers=headers)
    assert r.status_code == 204, r.text
    r = await client.post(url, json={"option_id": "yes"}, headers=headers)
    assert r.status_code == 404  # 答过就不再等

    frames = await asyncio.wait_for(turn, timeout=30)
    names = [n for n, _ in frames]
    assert "permission_request" in names and names[-1] == "done"
    req = next(d for n, d in frames if n == "permission_request")
    assert req["request_id"] == rid and req["title"] == "Write out.txt"
    assert req["timeout_s"] == 300 and {o["id"] for o in req["options"]} >= {"yes"}
    resolved = next(d for n, d in frames if n == "permission_resolved")
    assert resolved["outcome"] == "allowed"
    assert _said(frames) == "wrote it"
    # 时间线不落库那张一次性的请求卡
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    assert "permission_request" not in json.dumps(msgs)


async def test_a_disconnected_agent_turn_is_stored_as_interrupted(client):
    from sqlalchemy import select

    from app.agents.chat.events import DeltaEvent, ToolCallEvent
    from app.api.chat_agent import _stream_turn
    from app.core.db import get_sessionmaker
    from app.models.conversation import ConversationMessage
    from app.models.user import User
    from app.services import conversations as store
    from app.services.local_user import LOCAL_USER_EMAIL

    await register_and_login(client)
    async with get_sessionmaker()() as session:
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
        history = await store.replay(session, conversation_id=conv_id)
    # 回放里不留没结果的调用
    for msg in history:
        if isinstance(msg.content, list):
            assert not any(isinstance(b, ToolUseBlock) for b in msg.content)


async def test_the_router_stays_on_claude_with_agents_registered(client):
    """登记了智能体，Polaris 自己的环节仍然只走 Claude。"""
    from app.core.llm.router import get_llm_router

    headers = await _owner(client)
    await _register_fake(client, headers)
    for stage in ("agent", "relevance", "wiki_compile"):
        provider, route = await get_llm_router().resolve(stage)
        assert type(provider).__name__ != "AcpLLMProvider"
        assert getattr(route, "acp", None) is None
