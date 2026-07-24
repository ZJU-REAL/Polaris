"""内容池孤儿论文回收：彻底删除 / 每日推送过期时，论文不再被任何集合引用则删本体 + 文件。

被「库成员 / 课题书架 / 个人库 / 每日推送 / 论著」任一引用即保留；全无才回收。
"""

import datetime as dt
import tempfile
import uuid
from pathlib import Path

from app.core.db import get_sessionmaker
from app.models.daily_feed import DAILY_FEED_RETENTION_DAYS, DailyFeedEntry
from app.models.paper import Paper
from app.services import daily_feed as daily_feed_service
from tests.test_scoped_paper_detail import (
    _add_membership,
    _create_active_standalone,
    _hdr,
    _new_paper,
    _promote_admin,
)


async def _paper_exists(paper_id: str) -> bool:
    async with get_sessionmaker()() as session:
        return await session.get(Paper, uuid.UUID(paper_id)) is not None


async def _set_pdf_path(paper_id: str, path: str) -> None:
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        paper.pdf_path = path
        await session.commit()


async def _add_feed_entry(paper_id: str, feed_date: dt.date) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            DailyFeedEntry(
                paper_id=uuid.UUID(paper_id), feed_date=feed_date, primary_category="cs.AI"
            )
        )
        await session.commit()


async def _hard_delete(client, lib_id, paper_id, headers):
    return await client.post(
        f"/api/libraries/{lib_id}/papers/batch-delete",
        json={"paper_ids": [paper_id], "hard": True},
        headers=headers,
    )


async def test_hard_delete_last_reference_removes_pool_paper_and_files(client):
    """只在一个库、无其他引用：彻底删除连内容池本体 + 落盘文件一并回收。"""
    admin = await _hdr(client, "gc-admin@example.com")
    await _promote_admin("gc-admin@example.com")
    creator = await _hdr(client, "gc-owner@example.com")
    lib = await _create_active_standalone(client, creator, admin, name="唯一库")

    paper_id = await _new_paper(title="Orphan me")
    await _add_membership(lib, paper_id, status="excluded", relevance=0.2)

    # 造一个真实落盘文件，验证彻底删除会清掉它
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name
    await _set_pdf_path(paper_id, pdf_path)

    resp = await _hard_delete(client, lib, paper_id, creator)
    assert resp.status_code == 200 and resp.json()["deleted"] == 1

    assert not await _paper_exists(paper_id)  # 内容池本体被回收
    assert not Path(pdf_path).exists()  # 落盘文件被清理


async def test_hard_delete_keeps_pool_paper_when_in_another_library(client):
    """还在另一个库里：彻底删除只删本库成员行，内容池本体与另一库成员行保留。"""
    admin = await _hdr(client, "gc2-admin@example.com")
    await _promote_admin("gc2-admin@example.com")
    creator = await _hdr(client, "gc2-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="删除库 A")
    lib_b = await _create_active_standalone(client, creator, admin, name="保留库 B")

    paper_id = await _new_paper(title="Shared, keep")
    await _add_membership(lib_a, paper_id, status="excluded", relevance=0.2)
    await _add_membership(lib_b, paper_id, status="included", relevance=0.8)

    resp = await _hard_delete(client, lib_a, paper_id, creator)
    assert resp.status_code == 200 and resp.json()["deleted"] == 1

    assert await _paper_exists(paper_id)  # 别的库还在引用 → 本体保留
    resp = await client.get(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200 and resp.json()["status"] == "included"


async def test_hard_delete_keeps_pool_paper_when_in_daily_feed(client):
    """还在每日推送里：彻底删除保留内容池本体（推送引用算在用）。"""
    admin = await _hdr(client, "gc3-admin@example.com")
    await _promote_admin("gc3-admin@example.com")
    creator = await _hdr(client, "gc3-owner@example.com")
    lib = await _create_active_standalone(client, creator, admin, name="库 + 推送")

    paper_id = await _new_paper(title="Also in feed")
    await _add_membership(lib, paper_id, status="excluded", relevance=0.2)
    await _add_feed_entry(paper_id, dt.date(2026, 7, 24))

    resp = await _hard_delete(client, lib, paper_id, creator)
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    assert await _paper_exists(paper_id)  # 推送仍引用 → 保留


async def test_daily_feed_expiry_gcs_orphan_but_keeps_referenced(client):
    """推送过期清理：只在推送里的孤儿被回收；被库引用的保留。"""
    admin = await _hdr(client, "gc4-admin@example.com")
    await _promote_admin("gc4-admin@example.com")
    creator = await _hdr(client, "gc4-owner@example.com")
    lib = await _create_active_standalone(client, creator, admin, name="推送保留库")

    today = dt.date(2026, 7, 24)
    stale = today - dt.timedelta(days=DAILY_FEED_RETENTION_DAYS)  # 已过期

    orphan_id = await _new_paper(title="Feed-only orphan")
    await _add_feed_entry(orphan_id, stale)

    kept_id = await _new_paper(title="Feed + library")
    await _add_feed_entry(kept_id, stale)
    await _add_membership(lib, kept_id, status="included", relevance=0.7)

    async with get_sessionmaker()() as session:
        expired = await daily_feed_service.cleanup_expired(session, today=today)
        await session.commit()
    assert expired == 2  # 两条过期 entry 都删了

    assert not await _paper_exists(orphan_id)  # 只在推送 → 回收
    assert await _paper_exists(kept_id)  # 库仍引用 → 保留
