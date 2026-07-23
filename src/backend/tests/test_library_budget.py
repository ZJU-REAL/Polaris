"""P6 库预算（任务 2）：用量按库归因、月度聚合、超限拒绝启动与剩余额度收紧。"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary
from app.models.llm_config import LLMUsage
from app.models.voyage import VoyageRun
from app.services import ingest as ingest_service
from tests.conftest import register_and_login


async def _setup_project(client, email="budget-owner@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "预算方向"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        library = (
            await session.execute(
                select(DirectionLibrary).where(
                    DirectionLibrary.project_id == uuid.UUID(project_id)
                )
            )
        ).scalar_one()
        library_id = library.id
    return headers, project_id, library_id


async def _add_usage(library_id, *, prompt=0, completion=0, created_at=None, stage="librarian"):
    async with get_sessionmaker()() as session:
        row = LLMUsage(
            library_id=library_id,
            stage=stage,
            model="fake",
            prompt_tokens=prompt,
            completion_tokens=completion,
        )
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
        await session.commit()


async def _set_budget(library_id, monthly_budget):
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.monthly_budget = monthly_budget
        await session.commit()


async def test_monthly_usage_aggregation(client):
    _headers, _project_id, library_id = await _setup_project(client)
    other = uuid.uuid4()  # 无关归因（不同库/无库）不计入
    await _add_usage(library_id, prompt=1000, completion=500)
    await _add_usage(library_id, prompt=200, completion=300)
    # 上月的用量不计入本月
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=2)
    await _add_usage(library_id, prompt=99999, created_at=last_month)
    async with get_sessionmaker()() as session:
        session.add(LLMUsage(stage="forge", model="fake", prompt_tokens=777, completion_tokens=0))
        await session.commit()
        usage = await ingest_service.monthly_library_usage(session, library_id)
        assert usage["prompt_tokens"] == 1200
        assert usage["completion_tokens"] == 800
        assert usage["total_tokens"] == 2000
        # 其他库不受影响
        empty = await ingest_service.monthly_library_usage(session, other)
        assert empty["total_tokens"] == 0


async def test_ingest_rejected_when_budget_exhausted(client, queue_stub):
    headers, project_id, library_id = await _setup_project(client, email="budget-a@example.com")
    await _set_budget(library_id, 1000)
    await _add_usage(library_id, prompt=800, completion=300)  # 1100 ≥ 1000
    resp = await client.post(
        f"/api/projects/{project_id}/ingest", json={"mode": "bootstrap"}, headers=headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "LIBRARY_BUDGET_EXHAUSTED"
    assert queue_stub.jobs == []  # 没有入队任何任务
    # 预算面板显示已用尽
    resp = await client.get(f"/api/libraries/{library_id}/budget", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["exhausted"] is True
    assert body["used_tokens"] == 1100
    assert body["remaining_tokens"] == 0


async def test_ingest_budget_capped_to_monthly_remaining(client, queue_stub):
    headers, project_id, library_id = await _setup_project(client, email="budget-b@example.com")
    await _set_budget(library_id, 100_000)
    await _add_usage(library_id, prompt=20_000, completion=10_000)  # 已用 30k → 剩 70k
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": {"max_papers": 10}},  # 派生预算 200k > 剩余
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(voyage_id))
        assert run.budget["max_tokens"] == 70_000
    # 无预算库不受影响：max_tokens 用 knobs 派生值
    headers2, project_id2, _library_id2 = await _setup_project(client, email="budget-c@example.com")
    resp = await client.post(
        f"/api/projects/{project_id2}/ingest",
        json={"mode": "bootstrap", "knobs": {"max_papers": 10}},
        headers=headers2,
    )
    assert resp.status_code == 201, resp.text
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(resp.json()["id"]))
        assert run.budget["max_tokens"] == 200_000


async def test_budget_endpoint_permissions(client):
    headers, _project_id, library_id = await _setup_project(client, email="budget-d@example.com")
    stranger_token = await register_and_login(client, email="budget-stranger@example.com")
    stranger = {"Authorization": f"Bearer {stranger_token}"}
    resp = await client.get(f"/api/libraries/{library_id}/budget", headers=stranger)
    assert resp.status_code == 403
    resp = await client.get(f"/api/libraries/{library_id}/budget", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["monthly_budget"] is None
    assert body["remaining_tokens"] is None
    assert body["exhausted"] is False


async def test_router_records_library_attribution(client):
    """core/llm 记账入口带 library_id → LLMUsage 落库归因（fake provider）。"""
    _headers, _project_id, library_id = await _setup_project(client, email="budget-e@example.com")
    router = get_llm_router()
    await router.complete(
        "relevance",
        [Message(role="user", content="score this paper")],
        library_id=library_id,
    )
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(LLMUsage).where(LLMUsage.library_id == library_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].stage == "relevance"
        assert rows[0].prompt_tokens > 0
