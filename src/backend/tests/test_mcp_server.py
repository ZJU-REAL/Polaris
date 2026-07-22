"""MCP over HTTP：POST /mcp（JSON-RPC 2.0）—— initialize / tools.list / tools.call + 鉴权隔离。"""

import json
import uuid

from app.core.db import get_sessionmaker
from tests.conftest import add_paper

from .conftest import register_and_login


async def _setup(client, email="mcp@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "mcp-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        session.add(
            await add_paper(session,
                project_id=uuid.UUID(project_id),
                source="manual",
                title="MCP retrieval paper",
                abstract="retrieval over mcp tools",
                tldr="mcp paper",
                status="compiled",
            )
        )
        await session.commit()
    return project_id, headers


async def test_requires_auth(client):
    resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401


async def test_initialize_and_tools_list(client):
    _, headers = await _setup(client)

    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"]["serverInfo"]["name"] == "polaris"
    assert "protocolVersion" in body["result"]

    # 通知无响应体
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert resp.status_code == 202

    resp = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers
    )
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"search_papers", "get_concept", "external_search"} <= names
    # 每个工具 inputSchema 都追加了必填 project_id
    for t in tools:
        assert "project_id" in t["inputSchema"]["properties"]
        assert "project_id" in t["inputSchema"]["required"]


async def test_tools_call(client):
    project_id, headers = await _setup(client)

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_papers",
                "arguments": {"project_id": project_id, "query": "retrieval"},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert any(p["title"] == "MCP retrieval paper" for p in payload["results"])


async def test_tools_call_missing_project_id(client):
    _, headers = await _setup(client)
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "search_papers", "arguments": {"query": "x"}},
        },
        headers=headers,
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "project_id" in result["content"][0]["text"]


async def test_tools_call_cross_project_denied(client):
    """B 用户拿 A 项目 id 调用 → 非成员，视为项目不存在。"""
    project_a, _ = await _setup(client, email="owner-a@example.com")
    _, headers_b = await _setup(client, email="owner-b@example.com")

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "search_papers",
                "arguments": {"project_id": project_a, "query": "retrieval"},
            },
        },
        headers=headers_b,
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "无权访问" in result["content"][0]["text"]


async def test_catalog_endpoint(client):
    """GET /api/mcp/tools：前端「MCP 工具」页用的只读目录。"""
    _, headers = await _setup(client)
    resp = await client.get("/api/mcp/tools", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["server"]["name"] == "polaris"
    assert body["endpoint"] == "/mcp"
    names = {t["name"] for t in body["tools"]}
    assert len(names) >= 18
    assert {"get_paper_figure", "list_paper_figures", "find_figures"} <= names
    search = next(t for t in body["tools"] if t["name"] == "search_papers")
    assert any(p["name"] == "query" and p["required"] for p in search["params"])
    # 需登录
    assert (await client.get("/api/mcp/tools")).status_code == 401


async def test_unknown_method(client):
    _, headers = await _setup(client)
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "bogus/method"},
        headers=headers,
    )
    assert resp.json()["error"]["code"] == -32601
