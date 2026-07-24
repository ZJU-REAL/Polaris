"""每日新论文池：同步/过期清理（service 层）+ 浏览/点赞/收录/分类（API 层）。"""

import datetime as dt
import uuid

import pytest

from tests.conftest import make_project_with_library, register_and_login

pytestmark = pytest.mark.asyncio


def _rss_entry(arxiv_id: str, title: str, *, announce: str = "new") -> dict:
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": f"Abstract of {title}",
        "authors": [{"name": "Ada Lovelace"}],
        "published": "2026-07-24T00:00:00+00:00",
        "updated": None,
        "year": 2026,
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
        "doi": None,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "announce_type": announce,
    }


class _StubArxiv:
    """按分类返回固定 RSS 条目的假客户端。"""

    def __init__(self, by_category: dict[str, list[dict]]):
        self.by_category = by_category

    async def fetch_new(self, category: str) -> list[dict]:
        return self.by_category.get(category, [])


async def _run_sync(monkeypatch, by_category: dict[str, list[dict]]) -> dict:
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _StubArxiv(by_category))
    async with get_sessionmaker()() as session:
        return await daily_feed.sync_daily_feed(session)


async def test_sync_idempotent_and_cross_merge(client, monkeypatch):
    await register_and_login(client)
    feed = {
        "cs.AI": [_rss_entry("2607.00001", "Paper A"), _rss_entry("2607.00002", "Paper B")],
        # 同一篇论文在另一分类以 cross 出现 → 合并 categories，不重复建行
        "cs.CL": [_rss_entry("2607.00001", "Paper A", announce="cross")],
        "cs.CV": [],
    }
    result = await _run_sync(monkeypatch, feed)
    assert result["fetched"] == 3
    assert result["created"] == 2

    # 同日重跑幂等
    result2 = await _run_sync(monkeypatch, feed)
    assert result2["created"] == 0

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(DailyFeedEntry))).scalars().all()
        assert len(rows) == 2
        merged = next(r for r in rows if "cs.CL" in (r.categories or []))
        assert set(merged.categories) == {"cs.AI", "cs.CL"}


async def test_cleanup_expired_keeps_paper_and_membership(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00010", "Old Paper")]})

    resp = await client.get("/api/daily/papers", headers=headers)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    entry_id, paper_id = item["entry_id"], item["paper_id"]

    # 点个赞 + 收进个人库
    resp = await client.put(f"/api/daily/papers/{entry_id}/like", headers=headers)
    assert resp.status_code == 200
    resp = await client.post(
        "/api/daily/collect", json={"paper_ids": [paper_id], "personal": True}, headers=headers
    )
    assert resp.status_code == 200

    # 把 entry 改成 8 天前 → 再同步 → entry 与赞消失，Paper 与个人库条目仍在
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry, DailyFeedLike
    from app.models.library import UserLibraryEntry
    from app.models.paper import Paper

    async with get_sessionmaker()() as session:
        entry = await session.get(DailyFeedEntry, uuid.UUID(entry_id))
        entry.feed_date = entry.feed_date - dt.timedelta(days=8)
        await session.commit()

    result = await _run_sync(monkeypatch, {"cs.AI": []})
    assert result["expired"] == 1

    async with get_sessionmaker()() as session:
        assert await session.get(DailyFeedEntry, uuid.UUID(entry_id)) is None
        likes = (await session.execute(select(DailyFeedLike))).scalars().all()
        assert likes == []
        assert await session.get(Paper, uuid.UUID(paper_id)) is not None
        saved = (await session.execute(select(UserLibraryEntry))).scalars().all()
        assert len(saved) == 1 and saved[0].saved

    # 「我赞过的」也随之清空
    resp = await client.get("/api/daily/liked", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_like_toggle_facepile_and_sort(client, monkeypatch):
    token_a = await register_and_login(client)  # 首个 = admin
    token_b = await register_and_login(client, email="bob@example.com")
    ha = {"Authorization": f"Bearer {token_a}"}
    hb = {"Authorization": f"Bearer {token_b}"}
    await _run_sync(
        monkeypatch,
        {"cs.AI": [_rss_entry("2607.00021", "Hot Paper"), _rss_entry("2607.00022", "Cold Paper")]},
    )
    resp = await client.get("/api/daily/papers", headers=ha)
    hot = next(i for i in resp.json()["items"] if i["title"] == "Hot Paper")
    eid = hot["entry_id"]

    # 双人点赞；重复点幂等
    r1 = await client.put(f"/api/daily/papers/{eid}/like", headers=ha)
    assert r1.json()["like_count"] == 1 and r1.json()["liked_by_me"] is True
    await client.put(f"/api/daily/papers/{eid}/like", headers=hb)
    r2 = await client.put(f"/api/daily/papers/{eid}/like", headers=hb)
    assert r2.json()["like_count"] == 2

    # facepile：本人永远排最前（display_name 夹具里都叫 Alice，按 id 断言）
    preview = r2.json()["likers_preview"]
    assert len(preview) == 2
    me = await client.get("/api/users/me", headers=hb)
    assert preview[0]["id"] == me.json()["id"]

    # 默认按赞数排序：Hot 在前
    resp = await client.get("/api/daily/papers", headers=ha)
    assert resp.json()["items"][0]["title"] == "Hot Paper"
    assert resp.json()["items"][0]["like_count"] == 2

    # 完整名单
    resp = await client.get(f"/api/daily/papers/{eid}/likers", headers=ha)
    assert resp.status_code == 200 and len(resp.json()) == 2

    # 取消赞
    r3 = await client.delete(f"/api/daily/papers/{eid}/like", headers=hb)
    assert r3.json()["like_count"] == 1 and r3.json()["liked_by_me"] is False
    resp = await client.get("/api/daily/liked", headers=hb)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/daily/liked", headers=ha)
    assert resp.json()["total"] == 1


async def test_collect_to_library_topic_personal(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="daily-proj")
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00031", "Collect Me")]})
    resp = await client.get("/api/daily/papers", headers=headers)
    item = resp.json()["items"][0]

    payload = {
        "paper_ids": [item["paper_id"]],
        "direction_library_ids": [str(library_id)],
        "topic_ids": [project_id],
        "personal": True,
    }
    resp = await client.post("/api/daily/collect", json=payload, headers=headers)
    assert resp.status_code == 200
    results = {r["target_type"]: r for r in resp.json()["results"]}
    assert results["library"]["added"] == 1 and not results["library"]["forbidden"]
    assert results["topic"]["added"] == 1
    # 入架必入个人库（add_to_shelf 自带同步），个人库目标看到的是「已存在」
    assert results["personal"]["added"] + results["personal"]["skipped_existing"] == 1

    # 重复收录 → skipped_existing
    resp = await client.post("/api/daily/collect", json=payload, headers=headers)
    results = {r["target_type"]: r for r in resp.json()["results"]}
    assert results["library"]["skipped_existing"] == 1
    assert results["topic"]["skipped_existing"] == 1
    assert results["personal"]["skipped_existing"] == 1

    # 成员行 status=included
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import LibraryPaper

    async with get_sessionmaker()() as session:
        membership = (
            await session.execute(select(LibraryPaper).where(LibraryPaper.library_id == library_id))
        ).scalar_one()
        assert membership.status == "included"

    # collections 预勾选口径
    resp = await client.get(f"/api/daily/papers/{item['entry_id']}/collections", headers=headers)
    data = resp.json()
    assert str(library_id) in data["direction_library_ids"]
    assert project_id in data["topic_ids"]
    assert data["in_personal"] is True

    # 非成员课题 → forbidden，不整体失败
    other = await register_and_login(client, email="eve@example.com")
    oh = {"Authorization": f"Bearer {other}"}
    resp = await client.post(
        "/api/daily/collect",
        json={"paper_ids": [item["paper_id"]], "topic_ids": [project_id]},
        headers=oh,
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["forbidden"] is True


async def test_categories_admin_and_refresh(client, queue_stub):
    admin = await register_and_login(client)  # 首个 = admin
    member = await register_and_login(client, email="bob2@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    mh = {"Authorization": f"Bearer {member}"}

    resp = await client.get("/api/daily/categories", headers=mh)
    assert resp.json()["categories"] == ["cs.AI", "cs.CL", "cs.CV"]

    # 普通成员改分类 → 403
    resp = await client.put("/api/daily/categories", json={"categories": ["cs.LG"]}, headers=mh)
    assert resp.status_code == 403

    # admin 改分类；非法格式 422
    resp = await client.put(
        "/api/daily/categories", json={"categories": ["cs.LG", "stat.ML"]}, headers=ah
    )
    assert resp.status_code == 200 and resp.json()["categories"] == ["cs.LG", "stat.ML"]
    resp = await client.put(
        "/api/daily/categories", json={"categories": ["Not A Cat!"]}, headers=ah
    )
    assert resp.status_code == 422

    # 手动刷新入队（admin only）
    resp = await client.post("/api/daily/refresh", headers=mh)
    assert resp.status_code == 403
    resp = await client.post("/api/daily/refresh", headers=ah)
    assert resp.status_code == 202
    assert queue_stub.jobs and queue_stub.jobs[0][0] == "daily_feed_sync"
