"""外部 MCP 服务器的管理面（#754）。

这一层的要害只有两条，用例也围着它们转：

1. **门槛是 owner**——登记一台 stdio 服务器 = 声明「服务端进程可以拉起这条命令」。
   那是在服务器上执行任意程序的能力，多账号部署下这个风险属于所有人。
2. **env 只进不出**——写入加密落库，读取永不回传明文。回传等于给任何能打开管理面
   的人一份密钥副本，而管理面的意义只是改配置。
"""

import sys
from pathlib import Path

import pytest

from tests.conftest import register_and_login

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"

pytest.importorskip("mcp", reason="MCP SDK not installed")


def _payload(slug: str = "cae", **over) -> dict:
    body = {
        "slug": slug,
        "title": "假 CAE 服务器",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
        "env": {"LSDYNA_LICENSE": "super-secret"},
        "enabled": False,
    }
    body.update(over)
    return body


async def _owner(client):
    token = await register_and_login(client, email="owner@example.com")
    return {"Authorization": f"Bearer {token}"}


async def test_second_user_cannot_manage_servers(client):
    await register_and_login(client, email="owner@example.com")
    second = await register_and_login(client, email="second@example.com")
    headers = {"Authorization": f"Bearer {second}"}

    for method, url, body in [
        ("GET", "/api/mcp-servers", None),
        ("POST", "/api/mcp-servers", _payload()),
    ]:
        resp = await client.request(method, url, json=body, headers=headers)
        assert resp.status_code == 403, (url, resp.status_code)
        assert resp.json()["detail"] == "OWNER_REQUIRED"


async def test_anonymous_cannot_manage_servers(client):
    resp = await client.get("/api/mcp-servers")
    assert resp.status_code == 401


async def test_env_values_never_come_back(client):
    """只回键名，不回值。"""
    headers = await _owner(client)
    resp = await client.post("/api/mcp-servers", json=_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["env_keys"] == ["LSDYNA_LICENSE"]
    # 明文不该出现在响应的任何角落
    assert "super-secret" not in resp.text
    listed = await client.get("/api/mcp-servers", headers=headers)
    assert "super-secret" not in listed.text


async def test_env_is_encrypted_at_rest(client):
    """落库的是密文：备份、日志、误导出都不该带走许可证密钥。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.mcp_server import McpServer

    headers = await _owner(client)
    await client.post("/api/mcp-servers", json=_payload(), headers=headers)

    async with get_sessionmaker()() as session:
        row = (await session.execute(select(McpServer))).scalars().one()
    assert row.env_encrypted
    assert "super-secret" not in row.env_encrypted
    # 命令**不**加密：它是可审计的运行事实（这台服务器到底会执行什么）
    assert row.command == sys.executable


async def test_stdio_without_a_command_is_refused(client):
    headers = await _owner(client)
    resp = await client.post(
        "/api/mcp-servers", json=_payload(command=None), headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "STDIO_NEEDS_COMMAND"


async def test_http_without_a_url_is_refused(client):
    headers = await _owner(client)
    resp = await client.post(
        "/api/mcp-servers",
        json=_payload(transport="http", command=None, url=None),
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "HTTP_NEEDS_URL"


async def test_duplicate_slug_is_refused(client):
    """slug 是工具命名空间的一段，重名会让两台服务器的工具互相顶掉。"""
    headers = await _owner(client)
    await client.post("/api/mcp-servers", json=_payload(), headers=headers)
    resp = await client.post("/api/mcp-servers", json=_payload(), headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "SLUG_TAKEN"


async def test_enabling_connects_and_registers_the_tools(client):
    """端到端：启用 → 真连上假服务器 → 工具带命名空间进注册表。"""
    from app.tools.registry import known_tools

    headers = await _owner(client)
    resp = await client.post(
        "/api/mcp-servers", json=_payload(enabled=True), headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["last_error"] is None, body["last_error"]
    assert "mcp:cae:solve" in body["last_tools"]
    # 真进了注册表，而不只是记在行上
    assert "mcp:cae:solve" in known_tools()


async def test_disabling_removes_the_tools(client):
    from app.tools.registry import known_tools

    headers = await _owner(client)
    created = await client.post(
        "/api/mcp-servers", json=_payload(enabled=True), headers=headers
    )
    server_id = created.json()["id"]

    resp = await client.patch(
        f"/api/mcp-servers/{server_id}", json={"enabled": False}, headers=headers
    )
    assert resp.status_code == 200
    # 停用要把工具摘干净，否则 agent 还会去调一台已经关掉的服务器
    assert not any(n.startswith("mcp:cae:") for n in known_tools())


async def test_a_server_that_cannot_start_reports_instead_of_500(client):
    """外部软件连不上是常态：失败必须是数据，管理面要就地显示原因。"""
    headers = await _owner(client)
    resp = await client.post(
        "/api/mcp-servers",
        json=_payload(
            slug="broken",
            command=sys.executable,
            args=["-c", "raise SystemExit(1)"],
            enabled=True,
        ),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["last_error"], "连不上必须写进 last_error，而不是只留一条日志"
    assert body["last_tools"] == []


async def test_sync_on_a_disabled_server_is_refused(client):
    headers = await _owner(client)
    created = await client.post("/api/mcp-servers", json=_payload(), headers=headers)
    server_id = created.json()["id"]
    resp = await client.post(f"/api/mcp-servers/{server_id}/sync", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "SERVER_DISABLED"


async def test_deleting_a_server_removes_its_tools(client):
    from app.tools.registry import known_tools

    headers = await _owner(client)
    created = await client.post(
        "/api/mcp-servers", json=_payload(enabled=True), headers=headers
    )
    server_id = created.json()["id"]

    resp = await client.delete(f"/api/mcp-servers/{server_id}", headers=headers)
    assert resp.status_code == 204
    assert not any(n.startswith("mcp:cae:") for n in known_tools())
