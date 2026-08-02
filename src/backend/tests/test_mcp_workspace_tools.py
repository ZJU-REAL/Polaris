"""MCP 只读工具（工作区 / 文献库 / 稿件 / 实验日志）：走真实 MCP 协议路径。

这批工具是给外部 harness（Claude Code / Codex）用的：它接进来要先看清
「课题什么状态、语料从哪来、有什么在跑、卡在哪、稿子写到哪」。
每条用例都通过 ``POST /mcp`` 的 tools/call 调，确保测到的就是客户端看到的。
"""

import json
import uuid

from app.core.db import get_sessionmaker
from app.models.gate import Gate
from app.models.voyage import VoyageRun
from tests.conftest import add_paper

from .conftest import register_and_login


async def _setup(client, email="workspace@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "ws-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _call(client, headers, name: str, args: dict) -> dict:
    """调一个 MCP 工具，返回解析后的文本 payload（isError 时抛出，便于断言失败原因）。"""
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        raise AssertionError(f"{name} 报错：{text}")
    return json.loads(text)


async def _call_expect_error(client, headers, name: str, args: dict) -> str:
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is True, f"{name} 本该报错却成功了"
    return result["content"][0]["text"]


async def _seed_library(project_id: str) -> str:
    """课题的起源库是懒建的（首次收论文时才落行），文献库用例要先把它催出来。"""
    from tests.conftest import ensure_project_library

    async with get_sessionmaker()() as session:
        library = await ensure_project_library(session, uuid.UUID(project_id))
        await session.commit()
        return str(library.id)


async def _add_voyage(project_id: str, **fields) -> str:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind=fields.pop("kind", "idea_forge"),
            goal=fields.pop("goal", "生成候选想法"),
            status=fields.pop("status", "executing"),
            **fields,
        )
        session.add(run)
        await session.commit()
        return str(run.id)


# ---- 课题总览 ----


async def test_project_status(client):
    project_id, headers = await _setup(client)
    async with get_sessionmaker()() as session:
        session.add(
            await add_paper(
                session,
                project_id=uuid.UUID(project_id),
                source="manual",
                title="Retrieval paper",
                status="compiled",
            )
        )
        await session.commit()
    await _add_voyage(project_id, kind="idea_forge", status="executing")

    payload = await _call(client, headers, "get_project_status", {"project_id": project_id})
    assert payload["papers_total"] == 1
    assert payload["gates_pending"] == 0
    # 课题的语料来自关联文献库：建课题时会带一个起源库
    assert len(payload["source_libraries"]) >= 1
    assert [t["kind"] for t in payload["active_tasks"]] == ["idea_forge"]


# ---- 任务 ----


async def test_list_and_get_task(client):
    project_id, headers = await _setup(client, email="task@example.com")
    task_id = await _add_voyage(project_id, kind="idea_forge", status="executing")
    await _add_voyage(project_id, kind="paper_writing", status="done")

    listed = await _call(client, headers, "list_tasks", {"project_id": project_id})
    assert listed["total"] == 2
    running = await _call(
        client, headers, "list_tasks", {"project_id": project_id, "status": "running"}
    )
    assert [t["task_id"] for t in running["tasks"]] == [task_id]
    by_kind = await _call(
        client, headers, "list_tasks", {"project_id": project_id, "kind": "paper_writing"}
    )
    assert [t["kind"] for t in by_kind["tasks"]] == ["paper_writing"]

    detail = await _call(
        client, headers, "get_task", {"project_id": project_id, "task_id": task_id}
    )
    assert detail["kind"] == "idea_forge"
    assert detail["status"] == "executing"
    assert detail["blocked_on"] is None
    assert detail["last_error"] is None
    assert detail["steps"] == []


async def test_get_task_surfaces_blocking_gate(client):
    """任务停在人工闸门时，harness 必须能直接看到卡在哪个闸门上。"""
    project_id, headers = await _setup(client, email="gate-task@example.com")
    task_id = await _add_voyage(project_id, kind="experiment", status="paused_gate")
    async with get_sessionmaker()() as session:
        gate = Gate(
            project_id=uuid.UUID(project_id),
            kind="compute_budget",
            status="pending",
            payload={"voyage_id": task_id, "est_gpu_hours": 5.2},
            requested_by="voyage:experiment",
        )
        session.add(gate)
        await session.commit()
        gate_id = str(gate.id)

    detail = await _call(
        client, headers, "get_task", {"project_id": project_id, "task_id": task_id}
    )
    assert detail["blocked_on"]["gate_id"] == gate_id
    assert detail["blocked_on"]["kind"] == "compute_budget"
    assert detail["blocked_on"]["payload"]["est_gpu_hours"] == 5.2

    gates = await _call(client, headers, "list_gates", {"project_id": project_id})
    assert [g["gate_id"] for g in gates["gates"]] == [gate_id]
    assert gates["gates"][0]["task_id"] == task_id


async def test_task_cross_project_denied(client):
    """别的课题的任务：即便同一个人是两边成员，本次会话里也够不着。"""
    project_a, headers = await _setup(client, email="two-proj@example.com")
    task_a = await _add_voyage(project_a, kind="idea_forge")
    resp = await client.post("/api/projects", json={"name": "other"}, headers=headers)
    project_b = resp.json()["id"]

    message = await _call_expect_error(
        client, headers, "get_task", {"project_id": project_b, "task_id": task_a}
    )
    assert "不属于本课题" in message

    listed = await _call(client, headers, "list_tasks", {"project_id": project_b})
    assert listed["tasks"] == []


async def test_read_task_logs(client):
    project_id, headers = await _setup(client, email="logs@example.com")
    task_id = await _add_voyage(project_id)
    async with get_sessionmaker()() as session:
        from app.models.voyage import VoyageTerminalLog

        session.add_all(
            [
                VoyageTerminalLog(
                    run_id=uuid.UUID(task_id), event="log", level="info", message="开始生成"
                ),
                VoyageTerminalLog(
                    run_id=uuid.UUID(task_id), event="log", level="error", message="外部接口超时"
                ),
            ]
        )
        await session.commit()

    logs = await _call(
        client, headers, "read_task_logs", {"project_id": project_id, "task_id": task_id}
    )
    assert [row["message"] for row in logs["logs"]] == ["开始生成", "外部接口超时"]
    errors = await _call(
        client,
        headers,
        "read_task_logs",
        {"project_id": project_id, "task_id": task_id, "level": "error"},
    )
    assert [row["message"] for row in errors["logs"]] == ["外部接口超时"]


# ---- 文献库 ----


async def test_list_and_get_library(client):
    project_id, headers = await _setup(client, email="lib@example.com")
    await _seed_library(project_id)
    payload = await _call(client, headers, "list_libraries", {"project_id": project_id})
    assert payload["total"] >= 1
    linked = [lib for lib in payload["libraries"] if lib["linked_to_this_topic"]]
    assert linked, "课题的起源库应当算作已关联"
    library_id = linked[0]["library_id"]

    only_linked = await _call(
        client, headers, "list_libraries", {"project_id": project_id, "linked_only": True}
    )
    assert all(lib["linked_to_this_topic"] for lib in only_linked["libraries"])

    detail = await _call(
        client, headers, "get_library", {"project_id": project_id, "library_id": library_id}
    )
    assert detail["library_id"] == library_id
    assert "paper_count" in detail

    message = await _call_expect_error(
        client,
        headers,
        "get_library",
        {"project_id": project_id, "library_id": str(uuid.uuid4())},
    )
    assert "不存在或无权访问" in message


async def test_library_not_visible_to_stranger(client):
    """别人的个人库（非公共）：不可见与不存在同一口径，不泄露存在性。"""
    project_a, headers_a = await _setup(client, email="owner@example.com")
    resp = await client.post(
        "/api/libraries",
        json={"name": "owner-private", "statement": "只有我自己看得到的方向库"},
        headers=headers_a,
    )
    assert resp.status_code in (200, 201), resp.text
    private_id = resp.json()["id"]
    mine = await _call(client, headers_a, "list_libraries", {"project_id": project_a})
    assert private_id in [lib["library_id"] for lib in mine["libraries"]]

    project_b, headers_b = await _setup(client, email="stranger@example.com")
    message = await _call_expect_error(
        client, headers_b, "get_library", {"project_id": project_b, "library_id": private_id}
    )
    assert "不存在或无权访问" in message
    theirs = await _call(client, headers_b, "list_libraries", {"project_id": project_b})
    assert private_id not in [lib["library_id"] for lib in theirs["libraries"]]


# ---- 稿件 ----


async def test_manuscript_tools(client):
    project_id, headers = await _setup(client, email="ms@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/manuscripts",
        json={"title": "Retrieval Routing", "template": "neurips2026"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    manuscript_id = resp.json()["id"]

    listed = await _call(client, headers, "list_manuscripts", {"project_id": project_id})
    assert [m["manuscript_id"] for m in listed["manuscripts"]] == [manuscript_id]
    assert listed["manuscripts"][0]["review_passed"] is False

    detail = await _call(
        client,
        headers,
        "get_manuscript",
        {"project_id": project_id, "manuscript_id": manuscript_id},
    )
    paths = [f["path"] for f in detail["files"]]
    assert detail["main_tex"] in paths

    content = await _call(
        client,
        headers,
        "read_manuscript_file",
        {"project_id": project_id, "manuscript_id": manuscript_id, "path": detail["main_tex"]},
    )
    assert content["page"] == 0 and isinstance(content["text"], str)

    message = await _call_expect_error(
        client,
        headers,
        "read_manuscript_file",
        {"project_id": project_id, "manuscript_id": manuscript_id, "path": "nope.tex"},
    )
    assert "稿件里没有这个文件" in message


async def test_manuscript_cross_project_denied(client):
    project_a, headers = await _setup(client, email="ms-a@example.com")
    resp = await client.post(
        f"/api/projects/{project_a}/manuscripts",
        json={"title": "A", "template": "neurips2026"},
        headers=headers,
    )
    manuscript_id = resp.json()["id"]
    project_b = (
        await client.post("/api/projects", json={"name": "ms-b"}, headers=headers)
    ).json()["id"]

    message = await _call_expect_error(
        client,
        headers,
        "get_manuscript",
        {"project_id": project_b, "manuscript_id": manuscript_id},
    )
    assert "本课题内不存在该稿件" in message


# ---- 目录 ----


async def test_catalog_lists_the_new_tools(client):
    _, headers = await _setup(client, email="catalog@example.com")
    resp = await client.get("/api/mcp/tools", headers=headers)
    assert resp.status_code == 200, resp.text
    tools = {t["name"]: t for t in resp.json()["tools"]}
    for name in [
        "get_project_status",
        "list_tasks",
        "get_task",
        "read_task_logs",
        "list_gates",
        "list_libraries",
        "get_library",
        "search_daily_pool",
        "list_manuscripts",
        "get_manuscript",
        "read_manuscript_file",
        "read_experiment_logs",
    ]:
        assert name in tools, name
        assert tools[name]["read_only"] is True, name
