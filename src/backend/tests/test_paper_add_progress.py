"""手动添加文献的分阶段后台补全（task_id + enrich_paper 阶段事件 + SSE 鉴权），全离线。"""

import asyncio
import json
import time
import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx

from app.core.db import get_sessionmaker
from app.core.events import paper_task_channel
from app.services import paper_enrich
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import (
    add_paper,
    make_project_with_library,
    membership_of,
    register_and_login,
)

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/{aid}v1</id>
    <title>{title}</title>
    <summary>Some abstract text.</summary>
    <published>2026-06-01T00:00:00Z</published>
    <author><name>Alice Smith</name></author>
    <category term="cs.LG"/>
  </entry>
</feed>
"""


@pytest_asyncio.fixture
async def lit_clients():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    yield
    reset_clients()
    await redis.aclose()


# ---- 1. 入口 task_id：新论文非空 / 池命中已完整为 null ----


@respx.mock
async def test_manual_add_returns_task_id_for_new_paper(client, lit_clients, fake_redis):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="add-proj")
    feed = ARXIV_FEED.format(aid="2406.11111", title="New Paper")
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=feed)
    )
    respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(404))

    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"arxiv_id": "2406.11111"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task_id = body["task_id"]
    assert task_id  # 新建论文 → 启动了后台补全
    # 同步响应不含重活结果（异步补全）
    assert body["pdf_available"] is False

    await paper_enrich.await_task(task_id)  # 排空后台任务（触发 pdf 404 download）


async def test_manual_add_task_id_null_when_pool_hit_complete(client, fake_redis):
    """池命中且已处理完整（有 pdf/全文/embedding）→ task_id 为 null。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    proj_a, _ = await make_project_with_library(client, headers, name="seed-proj")
    proj_b, _ = await make_project_with_library(client, headers, name="target-proj")

    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=uuid.UUID(proj_a),
            title="Fully Processed Paper",
            doi="10.5555/complete",
            pdf_path="/tmp/x.pdf",
            full_text_path="/tmp/x.txt",
            embedding=[0.0] * 1024,
        )
        await session.commit()

    # 加同一 DOI 到另一课题：池命中、本库无成员行、论文已完整 → 不启动任务
    bibtex = "@article{c,\n title={Fully Processed Paper},\n doi={10.5555/complete},\n}"
    resp = await client.post(
        f"/api/projects/{proj_b}/papers", json={"bibtex": bibtex}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["task_id"] is None


# ---- 2. enrich_paper 阶段事件顺序 + 出错继续 + done ----


async def _collect_run_events(redis, *, task_id, **run_kwargs):
    pubsub = redis.pubsub()
    await pubsub.subscribe(paper_task_channel(task_id))
    runner = asyncio.create_task(
        paper_enrich._run_enrichment(task_id=task_id, redis=redis, **run_kwargs)
    )
    events: list[dict] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg is not None:
            payload = json.loads(msg["data"])
            events.append(payload)
            if payload["event"] in ("done", "error"):
                break
    await runner
    await pubsub.unsubscribe(paper_task_channel(task_id))
    await pubsub.aclose()
    return events


async def test_enrich_emits_all_stages_running_ok_and_done(client, fake_redis):
    """无 arxiv 论文（download/extract 跳过）：仍按顺序发 4 个阶段 + done，embed ok。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="enrich-proj")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Bibtex Only", doi="10.1/x"
        )
        await session.commit()
        paper_id = paper.id

    events = await _collect_run_events(
        fake_redis,
        task_id=uuid.uuid4().hex,
        paper_id=paper_id,
        library_id=None,
        user_id=None,
        project_id=None,
    )
    stage_events = [e["data"] for e in events if e["event"] == "stage"]
    # 每个阶段的取值都在 STAGES 内，且顺序与 STAGES 一致
    seen_order = [s["stage"] for s in stage_events]
    assert seen_order[0] == "download"  # 解析已在同步请求阶段完成，进度从下载起
    # embed 应有 ok（embed 走 fake provider）
    assert any(s["stage"] == "embed" and s["status"] in ("ok", "skipped") for s in stage_events)
    # 阶段整体顺序不倒序
    idx = [paper_enrich.STAGES.index(s) for s in seen_order]
    assert idx == sorted(idx)
    assert events[-1]["event"] == "done"


async def test_enrich_error_continues_and_still_done(client, fake_redis, monkeypatch):
    """embed 阶段抛错：发 embed/error，但 score 仍执行、最终仍 done。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="err-proj")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Boom Paper", doi="10.1/boom"
        )
        await session.commit()
        paper_id = paper.id

    async def boom(*args, **kwargs):
        raise RuntimeError("embed provider down")

    monkeypatch.setattr(paper_enrich, "embed_paper", boom)

    events = await _collect_run_events(
        fake_redis,
        task_id=uuid.uuid4().hex,
        paper_id=paper_id,
        library_id=library_id,
        user_id=None,
        project_id=uuid.UUID(project_id),
    )
    stage_events = [e["data"] for e in events if e["event"] == "stage"]
    assert any(s["stage"] == "embed" and s["status"] == "error" for s in stage_events)
    # embed 出错后 score 仍执行
    assert any(s["stage"] == "score" for s in stage_events)
    assert events[-1]["event"] == "done"


# ---- 3. SSE 端点鉴权：非归属用户拿不到流 ----


async def test_paper_task_events_auth(client, fake_redis):
    token_a = await register_and_login(client, email="owner@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    project_id, _ = await make_project_with_library(client, headers_a, name="sse-proj")
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"bibtex": "@article{s,\n title={SSE Paper},\n doi={10.9/sse},\n}"},
        headers=headers_a,
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]
    assert task_id
    await paper_enrich.await_task(task_id)

    # 非归属用户 → 404
    token_b = await register_and_login(client, email="intruder@example.com")
    resp = await client.get(
        f"/api/paper-tasks/{task_id}/events",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404

    # 不存在的 task_id → 404
    resp = await client.get(
        f"/api/paper-tasks/{uuid.uuid4().hex}/events", headers=headers_a
    )
    assert resp.status_code == 404


async def test_manual_add_scores_via_background_task(client, fake_redis):
    """打分已移入后台任务：同步响应 relevance_score 为 null，跑完任务后成员行落分。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="score-proj")
    bibtex = "@article{r,\n title={Relevant Study},\n author={Doe, J},\n year={2024},\n}"
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": bibtex}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["relevance_score"] is None  # 异步打分，同步响应未落分
    task_id = body["task_id"]
    assert task_id
    await paper_enrich.await_task(task_id)

    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=body["id"])
        assert membership.relevance_score is not None
        assert membership.status == "included"  # 人工纳入，打分不改状态


async def test_paper_task_events_replays_history_for_late_subscriber(client, fake_redis):
    """回归：任务在订阅前就发完事件（pub/sub 不补发）时，SSE 连上应回放历史 + done。

    这是"进度弹窗卡在处理中"的根因：迟到订阅者只订频道拿不到任何事件。
    """
    token = await register_and_login(client, email="late@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="replay-proj")
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"bibtex": "@article{lt,\n title={Late Sub},\n doi={10.9/late},\n}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]
    assert task_id
    # 任务彻底跑完（事件已全部发布并结束）之后，才去订阅 —— 复现竞态
    await paper_enrich.await_task(task_id)

    resp = await client.get(f"/api/paper-tasks/{task_id}/events", headers=headers)
    assert resp.status_code == 200
    body = resp.text
    # 回放应补齐阶段事件并以 done 收尾（无回放时流会空等心跳、永不结束）
    assert "event: stage" in body
    assert "event: done" in body
    assert "download" in body
