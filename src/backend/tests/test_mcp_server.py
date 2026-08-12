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
    assert {
        "list_accessible_projects",
        "search_papers",
        "get_concept",
        "external_search",
    } <= names
    # 用户级发现工具是 project_id 的获取入口，自身不能再要 project_id。
    for t in tools:
        if t["name"] == "list_accessible_projects":
            assert "project_id" not in t["inputSchema"]["properties"]
            assert "project_id" not in t["inputSchema"].get("required", [])
        else:
            assert "project_id" in t["inputSchema"]["properties"]
            assert "project_id" in t["inputSchema"]["required"]


async def test_list_accessible_projects_without_project_id(client):
    """发现工具按当前认证用户隔离，无需预先知道 project_id。"""
    # 首个注册用户会自动成为平台管理员（可见全部课题），先建自举账号。
    await _setup(client, email="project-list-admin@example.com")
    project_a, headers_a = await _setup(client, email="project-list-a@example.com")
    project_b, _ = await _setup(client, email="project-list-b@example.com")

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "list_accessible_projects", "arguments": {}},
        },
        headers=headers_a,
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    ids = {project["project_id"] for project in payload["projects"]}
    assert project_a in ids
    assert project_b not in ids
    assert payload["total_count"] == 1
    assert payload["has_more"] is False


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

    # scan_papers 可不带 query，直接按精确字段浏览和排序。
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "scan_papers",
                "arguments": {
                    "project_id": project_id,
                    "author": "missing author",
                    "sort": "-created_at",
                },
            },
        },
        headers=headers,
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["mode"] == "filtered" and payload["total"] == 0


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
    assert "list_accessible_projects" in result["content"][0]["text"]


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
    scan = next(t for t in body["tools"] if t["name"] == "scan_papers")
    scan_params = {p["name"]: p for p in scan["params"]}
    assert scan_params["query"]["required"] is False
    assert {
        "author",
        "affiliation",
        "published_from",
        "published_to",
        "created_from",
        "created_to",
        "sort",
        "page",
        "limit",
    } <= scan_params.keys()
    # 需登录
    assert (await client.get("/api/mcp/tools")).status_code == 401


async def test_invoke_tool(client):
    """POST /api/mcp/tools/{name}/invoke：页面上的「试运行」，走的是真实 MCP 调用路径。"""
    project_id, headers = await _setup(client, email="invoke@example.com")

    resp = await client.post(
        "/api/mcp/tools/search_papers/invoke",
        json={"project_id": project_id, "arguments": {"query": "retrieval", "mode": "keyword"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_error"] is False
    assert body["duration_ms"] >= 0
    payload = json.loads(body["content"][0]["text"])
    assert any(p["title"] == "MCP retrieval paper" for p in payload["results"])


async def test_invoke_reports_tool_error(client):
    """工具自己报错（缺参数）→ is_error，错误消息原样回给页面。"""
    project_id, headers = await _setup(client, email="invoke-err@example.com")

    resp = await client.post(
        "/api/mcp/tools/search_papers/invoke",
        json={"project_id": project_id, "arguments": {}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_error"] is True
    assert "query" in body["content"][0]["text"]


async def test_invoke_unknown_tool_and_auth(client):
    project_id, headers = await _setup(client, email="invoke-404@example.com")
    resp = await client.post(
        "/api/mcp/tools/no_such_tool/invoke",
        json={"project_id": project_id, "arguments": {}},
        headers=headers,
    )
    assert resp.status_code == 404
    # 需登录
    resp = await client.post(
        "/api/mcp/tools/search_papers/invoke",
        json={"project_id": project_id, "arguments": {}},
    )
    assert resp.status_code == 401


async def test_invoke_cross_project_denied(client):
    """试运行同样过成员校验：拿别人的课题 id 一律当作不存在。"""
    project_a, _ = await _setup(client, email="invoke-a@example.com")
    _, headers_b = await _setup(client, email="invoke-b@example.com")

    resp = await client.post(
        "/api/mcp/tools/search_papers/invoke",
        json={"project_id": project_a, "arguments": {"query": "retrieval"}},
        headers=headers_b,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_error"] is True
    assert "无权访问" in body["content"][0]["text"]


async def test_selfcheck(client):
    """POST /api/mcp/selfcheck：把工具跑一遍，报告哪些还能用、哪些已失效。"""
    project_id, headers = await _setup(client, email="selfcheck@example.com")

    resp = await client.post("/api/mcp/selfcheck", json={"project_id": project_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {r["name"]: r for r in body["results"]}

    # 底层重构把工具搞挂了 → 这里会红：任何工具都不许 error
    broken = {n: r["detail"] for n, r in by_name.items() if r["status"] == "error"}
    assert not broken, f"MCP 工具已失效：{broken}"

    assert body["summary"]["total"] == len(by_name)
    assert body["summary"]["ok"] >= 5
    # 库内检索类工具有论文样本 → 必须真跑过
    assert by_name["search_papers"]["status"] == "ok"
    assert by_name["search_papers"]["arguments"]["query"]
    assert by_name["get_paper"]["status"] == "ok"
    # 联网工具默认不实测
    assert by_name["external_search"]["status"] == "skipped"
    # 没有稿件 → 跳过并说明原因，不算失败
    assert by_name["get_fact_pack"]["status"] == "skipped"
    assert "稿件" in by_name["get_fact_pack"]["detail"]
    # 没有图片 → 取图类工具跳过
    assert by_name["get_paper_figure"]["status"] == "skipped"


async def test_selfcheck_names_and_cross_project(client):
    """names 过滤只跑指定工具；非成员课题直接 404（报告里带样本 id，不能泄露）。"""
    project_a, headers_a = await _setup(client, email="sc-a@example.com")
    _, headers_b = await _setup(client, email="sc-b@example.com")

    resp = await client.post(
        "/api/mcp/selfcheck",
        json={"project_id": project_a, "names": ["list_concepts"]},
        headers=headers_a,
    )
    assert resp.status_code == 200, resp.text
    assert [r["name"] for r in resp.json()["results"]] == ["list_concepts"]

    resp = await client.post(
        "/api/mcp/selfcheck",
        json={"project_id": project_a, "names": ["list_concepts"]},
        headers=headers_b,
    )
    assert resp.status_code == 404, resp.text


async def test_unknown_method(client):
    _, headers = await _setup(client)
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "bogus/method"},
        headers=headers,
    )
    assert resp.json()["error"]["code"] == -32601
