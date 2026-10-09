"""ask 策略：权限请求摆给用户，用户答了才放行（#838）。"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from app.services.acp import client as acp_client
from app.services.acp.client import AcpClient, AgentSpec
from tests.conftest import register_and_login

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


def _spec() -> AgentSpec:
    return AgentSpec(name="fake", command=sys.executable, args=(FAKE,), permission_policy="ask")


async def _run_write(tmp_path, answer: str | None, *, cancel: bool = False) -> list[dict]:
    """跑一轮 "write"；看到 permission_request 时按 answer 回答（None = 不答）。"""
    client = AcpClient(_spec(), root=str(tmp_path))
    events: list[dict] = []
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        async for ev in client.prompt(sid, "write"):
            events.append(ev)
            if ev["type"] == "permission_request":
                assert client.pending_asks(sid) == [ev["request_id"]]
                if cancel:
                    await client.cancel(sid)
                elif answer is not None:
                    assert client.answer_permission(ev["request_id"], answer)
                    # 答过的不能再答
                    assert not client.answer_permission(ev["request_id"], answer)
    finally:
        await client.close()
    return events


async def test_approved_write_goes_through(tmp_path):
    events = await _run_write(tmp_path, "yes")
    req = next(e for e in events if e["type"] == "permission_request")
    assert req["kind"] == "edit" and req["title"] == "Write out.txt"
    assert [o["kind"] for o in req["options"]] == ["allow_once", "reject_once"]
    perm = next(e for e in events if e["type"] == "permission")
    assert perm["outcome"] == "allowed" and perm["request_id"] == req["request_id"]
    assert (tmp_path / "out.txt").read_text() == "written by agent"


async def test_declined_write_does_not_happen(tmp_path):
    events = await _run_write(tmp_path, "no")
    assert next(e for e in events if e["type"] == "permission")["outcome"] == "denied"
    assert not (tmp_path / "out.txt").exists()


async def test_unanswered_request_times_out_as_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_client, "ASK_TIMEOUT", 0.3)
    events = await _run_write(tmp_path, None)
    assert next(e for e in events if e["type"] == "permission")["outcome"] == "denied"
    assert not (tmp_path / "out.txt").exists()


async def test_cancelling_the_turn_releases_a_waiting_request(tmp_path):
    events = await _run_write(tmp_path, None, cancel=True)
    assert next(e for e in events if e["type"] == "permission")["outcome"] == "denied"
    assert not (tmp_path / "out.txt").exists()


async def test_unknown_option_is_rejected(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        async for ev in client.prompt(sid, "write"):
            if ev["type"] == "permission_request":
                assert not client.answer_permission(ev["request_id"], "made-up")
                assert not client.answer_permission("no-such-request", "yes")
                assert client.answer_permission(ev["request_id"], "no")
    finally:
        await client.close()


async def test_write_needs_a_fresh_grant(tmp_path):
    """ask 下一次「允许」只换一次写；没批准过的写被拒。"""
    client = AcpClient(_spec(), root=str(tmp_path))
    target = (tmp_path / "a.txt").resolve()
    assert not client._may_write("s1", target)
    client._write_grants["s1"] = [acp_client._WriteGrant("t1", None)]
    assert client._may_write("s1", target)
    assert not client._may_write("s1", target)
    client._always_write.add("s1")
    assert client._may_write("s1", target)


async def _ask_session(tmp_path) -> tuple[AcpClient, str]:
    client = AcpClient(_spec(), root=str(tmp_path))
    await client.start()
    return client, await client.new_session(str(tmp_path))


async def _grant(client: AcpClient, sid: str, tool: dict) -> None:
    """模拟 agent 发来一次 edit 权限请求、用户点了允许。"""
    client._sinks[sid] = asyncio.Queue()
    params = {
        "sessionId": sid,
        "toolCall": {"kind": "edit", **tool},
        "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}],
    }
    asking = asyncio.create_task(client._on_request("session/request_permission", params))
    for _ in range(100):
        if client.pending_asks(sid):
            break
        await asyncio.sleep(0.01)
    assert client.answer_permission(client.pending_asks(sid)[0], "yes")
    await asking


async def _write(client: AcpClient, sid: str, path: str) -> bool:
    from app.services.acp.connection import RpcMethodError

    params = {"sessionId": sid, "path": path, "content": "x"}
    try:
        await client._on_request("fs/write_text_file", params)
    except RpcMethodError:
        return False
    return True


async def test_a_grant_only_covers_the_file_it_named(tmp_path):
    """批准「改 a.txt」不能拿去改 b.txt；越界的写不消耗批准。"""
    client, sid = await _ask_session(tmp_path)
    try:
        await _grant(
            client,
            sid,
            {"toolCallId": "e1", "title": "Edit a.txt", "rawInput": {"file_path": "a.txt"}},
        )
        assert not await _write(client, sid, str(tmp_path / "b.txt"))
        # 越界：路径先被钉住，批准还在
        assert not await _write(client, sid, "/etc/evil.txt")
        assert await _write(client, sid, str(tmp_path / "a.txt"))
        assert not await _write(client, sid, str(tmp_path / "a.txt"))  # 一次批准一次写
        # ACP 的 locations 也算数
        await _grant(
            client, sid, {"toolCallId": "e2", "locations": [{"path": str(tmp_path / "c.txt")}]}
        )
        assert await _write(client, sid, str(tmp_path / "c.txt"))
    finally:
        await client.close()
    assert not (tmp_path / "b.txt").exists()


async def test_unused_grants_expire_with_their_tool_call_and_the_turn(tmp_path):
    client, sid = await _ask_session(tmp_path)
    try:
        await _grant(client, sid, {"toolCallId": "e1", "title": "Edit"})
        # 那次工具调用结束了：批准作废，不能挪给后面的写
        await client._on_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "e1",
                    "status": "completed",
                },
            },
        )
        assert not await _write(client, sid, str(tmp_path / "a.txt"))

        # 一轮结束：没用掉的批准不带进下一轮
        await _grant(client, sid, {"toolCallId": "e2", "title": "Edit"})
        assert client._write_grants[sid]
        _ = [e async for e in client.prompt(sid, "hello")]
        assert not await _write(client, sid, str(tmp_path / "a.txt"))
    finally:
        await client.close()


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


@pytest.fixture(autouse=True)
async def _clean_pool():
    yield
    from app.core.llm.acp import shutdown_pools
    from app.services.acp.pool import shutdown_pool

    await shutdown_pool()
    # 助手起标题等模型调用现在也会落到登记的 agent 上（#840），那组进程一并收掉
    await shutdown_pools()


async def test_the_panel_answers_a_request_over_the_api(client, agent_on):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    resp = await client.post(
        "/api/acp-agents",
        json={"slug": "fake", "command": sys.executable, "args": [FAKE]},
        headers=headers,
    )
    agent = resp.json()
    assert agent["permission_policy"] == "ask"  # 新登记默认 ask
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]

    # 测试用的 ASGI 传输会把流式响应攒到结束才交出来，所以不能边读边答：
    # 这一轮放到后台跑，从活会话上找到正在等的请求号，经接口回答。
    async def run_turn() -> str:
        resp = await client.post(
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": "write", "backend": agent["id"]},
            headers=headers,
        )
        return resp.text

    turn = asyncio.create_task(run_turn())
    from app.services.acp import chat as acp_chat

    rid = None
    for _ in range(200):
        live = acp_chat.live_session(uuid.UUID(conv_id))
        pending = live.client.pending_asks(live.session_id) if live else []
        if pending:
            rid = pending[0]
            break
        await asyncio.sleep(0.02)
    assert rid, "the agent never asked"
    url = f"/api/chat/conversations/{conv_id}/permissions/{rid}"
    r = await client.post(url, json={"option_id": "nope"}, headers=headers)
    assert r.status_code == 400, r.text
    r = await client.post(url, json={"option_id": "yes"}, headers=headers)
    assert r.status_code == 204, r.text
    # 答过就不再等
    r = await client.post(url, json={"option_id": "yes"}, headers=headers)
    assert r.status_code == 404

    body = await asyncio.wait_for(turn, timeout=30)
    frames: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    names = [n for n, _ in frames]
    assert "permission_request" in names and names[-1] == "done"
    req = next(d for n, d in frames if n == "permission_request")
    assert req["request_id"] == rid and req["title"] == "Write out.txt"
    resolved = next(d for n, d in frames if n == "permission_resolved")
    assert resolved["outcome"] == "allowed"
    text = "".join(d["text"] for n, d in frames if n == "delta")
    assert text == "wrote it"
    # 时间线不落库那张一次性的请求卡
    msgs = (await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)).json()
    assert "permission_request" not in json.dumps(msgs)
