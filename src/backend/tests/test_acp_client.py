"""ACP client 对着一个说真协议的假 agent 跑（#836）。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.services.acp.client import (
    AcpClient,
    AgentSpec,
    contained_path,
    decide_permission,
    inherited_env,
    normalize_update,
)
from app.services.acp.connection import AcpError, RpcMethodError
from app.services.acp.pool import AcpSessionPool

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


def _spec(policy: str = "deny", env: dict | None = None) -> AgentSpec:
    return AgentSpec(
        name="fake", command=sys.executable, args=(FAKE,), env=env or {}, permission_policy=policy
    )


async def _turn(client: AcpClient, sid: str, text: str) -> list[dict]:
    return [e async for e in client.prompt(sid, text)]


async def test_handshake_and_a_full_turn(tmp_path):
    (tmp_path / "notes.txt").write_text("the notes")
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        info = await client.start()
        assert info.name == "fake-agent" and info.protocol_version == 1
        assert info.load_session and info.mcp_http
        sid = await client.new_session(str(tmp_path))
        events = await _turn(client, sid, "hello")
    finally:
        await client.close()

    kinds = [e["type"] for e in events]
    assert kinds[0] == "thought" and kinds[-1] == "done"
    assert events[-1]["stop_reason"] == "end_turn"
    plan = next(e for e in events if e["type"] == "plan")
    assert [p["content"] for p in plan["entries"]] == ["Read notes", "Answer"]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["kind"] == "read" and call["title"] == "Read notes.txt"
    result = next(e for e in events if e["type"] == "tool_update")
    # agent 经我们的 fs/read_text_file 读到了工作目录里的文件
    assert result["status"] == "completed" and result["output"] == "the notes"
    assert "".join(e["text"] for e in events if e["type"] == "message") == "echo: hello"


async def test_reads_outside_the_workdir_are_refused(tmp_path):
    client = AcpClient(_spec(policy="auto"), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        events = await _turn(client, sid, "escape")
    finally:
        await client.close()
    said = json.loads("".join(e["text"] for e in events if e["type"] == "message"))
    assert "error" in said and "outside" in said["error"]["message"]


@pytest.mark.parametrize(
    ("policy", "written"),
    [("deny", False), ("read_only", False), ("auto", True)],
)
async def test_permission_policy_governs_edits(tmp_path, policy, written):
    client = AcpClient(_spec(policy=policy), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        events = await _turn(client, sid, "write")
    finally:
        await client.close()
    perm = next(e for e in events if e["type"] == "permission")
    assert perm["outcome"] == ("allowed" if written else "denied")
    assert (tmp_path / "out.txt").exists() is written


async def test_the_agent_does_not_inherit_server_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SECRET_KEY", "s3cret")
    client = AcpClient(_spec(env={"FAKE_AGENT_TOKEN": "mine"}), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        events = await _turn(client, sid, "env")
    finally:
        await client.close()
    report = json.loads("".join(e["text"] for e in events if e["type"] == "message"))
    assert report == {"DATABASE_URL": False, "SECRET_KEY": False, "FAKE_AGENT_TOKEN": True}


async def test_abandoning_a_turn_cancels_it_on_the_agent(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        gen = client.prompt(sid, "slow")
        first = await gen.__anext__()
        assert first["text"] == "working…"
        await client.cancel(sid)
        rest = [e async for e in gen]
        assert rest[-1] == {"type": "done", "stop_reason": "cancelled"}
    finally:
        await client.close()


async def test_a_crashing_agent_surfaces_an_error(tmp_path):
    client = AcpClient(_spec(), root=str(tmp_path))
    try:
        await client.start()
        sid = await client.new_session(str(tmp_path))
        with pytest.raises(AcpError) as exc:
            await _turn(client, sid, "crash")
        assert exc.value.code == "not-running"
        assert not client.alive
    finally:
        await client.close()


async def test_missing_command_is_a_spawn_failure(tmp_path):
    client = AcpClient(
        AgentSpec(name="nope", command="definitely-not-a-real-agent-xyz"), root=str(tmp_path)
    )
    with pytest.raises(AcpError) as exc:
        await client.start()
    assert exc.value.code == "spawn-failed"
    await client.close()


# ---------------------------------------------------------------- 纯函数


def test_decide_permission_by_policy():
    opts = [
        {"optionId": "a", "kind": "allow_once"},
        {"optionId": "aa", "kind": "allow_always"},
        {"optionId": "r", "kind": "reject_once"},
    ]
    assert decide_permission("deny", "read", opts) == {"outcome": "selected", "optionId": "r"}
    assert decide_permission("read_only", "read", opts)["optionId"] == "a"
    assert decide_permission("read_only", "execute", opts)["optionId"] == "r"
    # auto 只选「这一次」，不选「总是」：每个动作仍然逐条经过策略
    assert decide_permission("auto", "execute", opts)["optionId"] == "a"
    assert decide_permission("deny", "edit", []) == {"outcome": "cancelled"}


def test_contained_path(tmp_path):
    (tmp_path / "sub").mkdir()
    assert contained_path(str(tmp_path), str(tmp_path / "sub" / "x.txt")).name == "x.txt"
    for bad in ("relative.txt", str(tmp_path / ".." / "x"), "/etc/passwd"):
        with pytest.raises(RpcMethodError):
            contained_path(str(tmp_path), bad)
    # 符号链接也绕不出去
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(RpcMethodError):
        contained_path(str(tmp_path), str(tmp_path / "link" / "passwd"))


def test_inherited_env_is_an_allowlist():
    env = inherited_env(
        {
            "PATH": "/bin",
            "HOME": "/h",
            "LC_ALL": "C",
            "https_proxy": "http://p",
            "DATABASE_URL": "x",
            "POLARIS_SECRET_KEY": "k",
            "OPENAI_API_KEY": "sk",
        }
    )
    assert env == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "https_proxy": "http://p"}


def test_normalize_update_shapes():
    assert normalize_update({"sessionUpdate": "available_commands_update"}) is None
    assert normalize_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    ) == {"type": "message", "text": "hi"}
    big = normalize_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t",
            "status": "completed",
            "rawOutput": "x" * 10_000,
        }
    )
    assert big is not None and len(big["output"]) < 5000  # 截断，免得灌爆 SSE


# ---------------------------------------------------------------- 池


async def test_pool_reuses_and_resumes_sessions(tmp_path):
    import uuid

    pool = AcpSessionPool(idle_ttl=0.0)
    agent_id = uuid.uuid4()
    try:
        live = await pool.open("c1", agent_id=agent_id, spec=_spec(), cwd=str(tmp_path))
        again = await pool.open("c1", agent_id=agent_id, spec=_spec(), cwd=str(tmp_path))
        assert again is live  # 同一个对话复用同一个进程
        old_sid = live.session_id

        assert await pool.reap() == 1  # idle_ttl=0：空闲的立刻被收
        assert pool.get("c1") is None

        # 新进程用旧会话号续上（agent 支持 loadSession），回放的历史不进这一轮
        resumed = await pool.open(
            "c1", agent_id=agent_id, spec=_spec(), cwd=str(tmp_path), resume_session_id=old_sid
        )
        assert resumed.resumed and resumed.session_id == old_sid
        events = [e async for e in resumed.client.prompt(old_sid, "hello")]
        assert "REPLAYED HISTORY" not in "".join(e.get("text", "") for e in events)
    finally:
        await pool.close_all()


async def test_pool_forget_agent_closes_its_sessions(tmp_path):
    import uuid

    pool = AcpSessionPool()
    agent_id = uuid.uuid4()
    try:
        live = await pool.open("c1", agent_id=agent_id, spec=_spec(), cwd=str(tmp_path))
        await pool.forget_agent(agent_id)
        await asyncio.sleep(0)
        assert not live.client.alive and pool.get("c1") is None
    finally:
        await pool.close_all()
