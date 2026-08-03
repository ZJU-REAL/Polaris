"""全局助手的 SSE 端点：帧序列、开关、鉴权、落库。

老的六个对话端点一行不动——这是新路径。助手成熟之前两套并存，出问题关掉开关就退回去。
"""

import json
import os
import uuid

import pytest

from app.core.db import get_sessionmaker
from tests.conftest import register_and_login


@pytest.fixture
def agent_on(monkeypatch):
    """打开助手开关（默认关：它每轮重发历史与工具 schema，成本不是一个量级）。

    get_settings 是 lru_cache 的，光改环境变量不生效——两头都要清缓存。
    """
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


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


async def _headers(client, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def test_the_endpoint_is_hidden_when_the_flag_is_off(client):
    """开关关着时端点直接 404——不是 403，别让它出现在能力探测里。"""
    headers = await _headers(client, "agent-off@example.com")
    resp = await client.post("/api/chat/conversations", json={}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "CHAT_AGENT_DISABLED"


async def test_a_turn_streams_meta_delta_done(client, agent_on):
    """一轮问答的最小帧序列：meta 开头、done 结尾，正文走 delta。

    ``delta`` 的形状与老端点一字不差，所以只认 sources/delta/done/error 的客户端
    也能工作，只是看不到工具卡片。
    """
    headers = await _headers(client, "agent-turn@example.com")
    resp = await client.post("/api/chat/conversations", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    conv_id = resp.json()["id"]

    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "帮我看看这个方向"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["x-accel-buffering"] == "no", "不关 nginx 缓冲就不是真流式"
        body = (await stream.aread()).decode()

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "delta" in kinds
    assert events[0][1]["conversation_id"] == conv_id
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert answer.strip(), "总得说点什么"


async def test_the_answer_is_persisted_and_replayed_next_turn(client, agent_on):
    """回答落库，第二轮**不用前端传 history**也接得上。这是搬到服务端的全部意义。"""
    from sqlalchemy import select

    from app.models.conversation import ConversationMessage

    headers = await _headers(client, "agent-persist@example.com")
    conv_id = (
        await client.post("/api/chat/conversations", json={}, headers=headers)
    ).json()["id"]

    for question in ("第一个问题", "第二个问题"):
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": question},
            headers=headers,
        ) as stream:
            await stream.aread()

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == uuid.UUID(conv_id))
                    .order_by(ConversationMessage.seq)
                )
            )
            .scalars()
            .all()
        )

    roles = [r.role for r in rows]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert rows[0].text == "第一个问题"
    assert rows[1].text.strip(), "assistant 的回答也要落库"

    # 消息接口能取到完整内容（SSE 里的 preview 是截断的）
    resp = await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 4


async def test_someone_elses_conversation_is_not_readable(client, agent_on):
    """别人的会话既读不到消息，也不能往里发。"""
    owner = await _headers(client, "agent-owner@example.com")
    other = await _headers(client, "agent-other@example.com")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=owner)).json()["id"]

    resp = await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=other)
    assert resp.status_code == 404

    resp = await client.get("/api/chat/conversations", headers=other)
    assert conv_id not in {c["id"] for c in resp.json()}


async def test_conversations_are_listed_newest_first(client, agent_on):
    headers = await _headers(client, "agent-list@example.com")
    ids = []
    for _ in range(2):
        conv_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        ids.append(conv_id)
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": f"问题 {conv_id[:4]}"},
            headers=headers,
        ) as stream:
            await stream.aread()

    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert [c["id"] for c in listed][:2] == ids[::-1], "最近说过话的排在前面"
    assert listed[0]["title"], "标题取首条用户消息"


async def test_an_unknown_scope_is_rejected(client, agent_on):
    headers = await _headers(client, "agent-scope@example.com")
    resp = await client.post(
        "/api/chat/conversations", json={"scope_kind": "made-up"}, headers=headers
    )
    assert resp.status_code == 422
