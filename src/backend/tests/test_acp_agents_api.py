"""外部 agent 的管理面 + 助手对话交给外部 agent（#836）。"""

import json
import os
import sys
from pathlib import Path

import pytest

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


async def test_templates_list_known_agents(client):
    headers = await _owner(client)
    resp = await client.get("/api/acp-agents/templates", headers=headers)
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert {"claude-code", "codex", "gemini", "qwen", "opencode", "kimi"} <= ids
    claude = next(t for t in resp.json() if t["id"] == "claude-code")
    assert claude["command"] == "claude-agent-acp" and "installed" in claude


async def test_crud_probe_and_env_never_returned(client):
    headers = await _owner(client)
    row = await _register_fake(client, headers, env={"FAKE_AGENT_TOKEN": "secret-value"})
    assert row["permission_policy"] == "deny"  # 默认最保守
    assert row["env_keys"] == ["FAKE_AGENT_TOKEN"] and "secret-value" not in json.dumps(row)
    assert row["command_found"] is True

    probed = (await client.post(f"/api/acp-agents/{row['id']}/probe", headers=headers)).json()
    assert probed["last_error"] is None
    assert probed["last_probe"]["name"] == "fake-agent"

    resp = await client.patch(
        f"/api/acp-agents/{row['id']}", json={"permission_policy": "nonsense"}, headers=headers
    )
    assert resp.status_code == 400
    resp = await client.patch(
        f"/api/acp-agents/{row['id']}", json={"args": ["/no/such/script.py"]}, headers=headers
    )
    assert resp.json()["last_probe"] is None  # 拉起方式变了，旧探测作废

    assert (await client.delete(f"/api/acp-agents/{row['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/acp-agents", headers=headers)).json() == []


async def test_probe_failure_is_data_with_an_install_hint(client):
    headers = await _owner(client)
    resp = await client.post(
        "/api/acp-agents",
        json={
            "slug": "claude",
            "template": "claude-code",
            "command": "claude-agent-acp-not-installed-xyz",
        },
        headers=headers,
    )
    row = resp.json()
    assert row["command_found"] is False
    probed = (await client.post(f"/api/acp-agents/{row['id']}/probe", headers=headers)).json()
    assert "npm install -g @agentclientprotocol/claude-agent-acp" in probed["last_error"]


async def test_only_the_owner_manages_agents(client):
    await _owner(client)
    other = {
        "Authorization": f"Bearer {await register_and_login(client, email='other@example.com')}"
    }
    assert (await client.get("/api/acp-agents", headers=other)).status_code == 403


async def test_assistant_turn_runs_on_the_agent(client, agent_on, tmp_path):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)

    backends = (await client.get("/api/chat/backends", headers=headers)).json()
    assert [b["id"] for b in backends] == ["polaris", agent["id"]]

    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "what do the notes say", "backend": agent["id"]},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    events = _parse_sse(body)
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert events[0][1]["model"] == "Fake Agent"
    assert {"thinking", "plan", "tool_call", "tool_result", "delta"} <= set(names)
    call = next(d for e, d in events if e == "tool_call")
    assert call["name"] == "agent_read" and call["args"]["title"] == "Read notes.txt"
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == "echo: what do the notes say"

    # 落库：整条时间线在，而且会话记住了 agent 那边的会话号
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    assert msgs[-1]["role"] == "assistant" and "echo:" in msgs[-1]["text"]
    import uuid

    from app.core.db import get_sessionmaker
    from app.models.conversation import Conversation

    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, uuid.UUID(conv_id))
        assert conv is not None
        assert conv.settings["backend"] == agent["id"]
        assert conv.settings["acp_agent_id"] == agent["id"]
        assert conv.settings["acp_session_id"].startswith("sess-")
    # 工作目录按对话隔离，在 data_dir 下
    assert any((tmp_path / "acp-workspaces").rglob(conv_id))

    # 第二轮不传 backend：沿用会话上存的那个，同一个进程
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "again"},
        headers=headers,
    ) as resp:
        body2 = "".join([chunk async for chunk in resp.aiter_text()])
    text2 = "".join(d["text"] for e, d in _parse_sse(body2) if e == "delta")
    assert text2 == "echo: again"


async def test_new_process_gets_earlier_turns_as_context(client, agent_on, monkeypatch):
    """进程被回收、agent 又不支持续会话时，把之前的对话摘要补进提示词。"""
    monkeypatch.setenv("FAKE_ACP_NO_LOAD", "1")
    headers = await _owner(client)
    agent = await _register_fake(client, headers, env={"FAKE_ACP_NO_LOAD": "1"})
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]

    async def turn(question: str) -> str:
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": question, "backend": agent["id"]},
            headers=headers,
        ) as resp:
            body = "".join([chunk async for chunk in resp.aiter_text()])
        return "".join(d["text"] for e, d in _parse_sse(body) if e == "delta")

    await turn("first question")
    from app.services.acp.pool import get_pool

    await get_pool().close(conv_id)  # 模拟空闲超时被回收
    # 假 agent 回显的是分隔线之后的部分；分隔线存在本身就说明摘要被补进去了
    assert await turn("second question") == "echo: second question"


async def test_unshared_agents_are_owner_only(client, agent_on):
    headers = await _owner(client)
    agent = await _register_fake(client, headers)
    other = {"Authorization": f"Bearer {await register_and_login(client, email='o2@example.com')}"}
    assert [b["id"] for b in (await client.get("/api/chat/backends", headers=other)).json()] == [
        "polaris"
    ]
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=other)).json()["id"]
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "hi", "backend": agent["id"]},
        headers=other,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "ACP_AGENT_NOT_AVAILABLE"

    await client.patch(f"/api/acp-agents/{agent['id']}", json={"shared": True}, headers=headers)
    ids = [b["id"] for b in (await client.get("/api/chat/backends", headers=other)).json()]
    assert agent["id"] in ids
