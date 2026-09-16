"""每日池的其它几道门也得按订阅走（#806）。

信息流那个查询加了过滤还不够：同一批论文从导出、从 agent 工具、从首页计数出去时，
用户看到的仍然是别人订的领域，而且和他信息流里的数字对不上。

这几条各盯一道门。它们不是「以防万一」——写的时候这三处确实都在读整池。
"""

import datetime as dt
import uuid

from app.core.db import get_sessionmaker
from app.models.daily_feed import DailyFeedEntry
from app.models.paper import Paper
from tests.conftest import register_and_login


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


async def _auth(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _add_entry(title: str, category: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        paper = Paper(source="arxiv", dedup_key=uuid.uuid4().hex, title=title, abstract=title)
        session.add(paper)
        await session.flush()
        session.add(
            DailyFeedEntry(
                paper_id=paper.id,
                feed_date=_today(),
                primary_category=category,
                categories=[category],
            )
        )
        await session.commit()
        return paper.id


async def _two_users_with_different_fields(client):
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    await client.put("/api/daily/categories", json={"categories": ["cs.AI"]}, headers=owner)
    await client.put("/api/daily/categories", json={"categories": ["q-bio.NC"]}, headers=member)
    await _add_entry("Agent planning", "cs.AI")
    mine = await _add_entry("Cortical circuits", "q-bio.NC")
    return owner, member, mine


async def test_citation_export_gives_you_your_own_papers(client):
    """界面上看到 1 篇、导出来是整池两篇别人领域的，那张 .bib 就是错的。"""
    _owner, member, _mine = await _two_users_with_different_fields(client)

    resp = await client.get("/api/daily/export/citations?format=bibtex", headers=member)
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Cortical circuits" in body
    assert "Agent planning" not in body


async def test_exporting_named_ids_still_works(client):
    """点名要哪几篇就按 id 给：这些 id 是他从自己界面上选出来的。"""
    _owner, member, mine = await _two_users_with_different_fields(client)

    resp = await client.get(
        f"/api/daily/export/citations?format=bibtex&ids={mine}", headers=member
    )
    assert resp.status_code == 200, resp.text
    assert "Cortical circuits" in resp.text


async def test_the_buddy_count_matches_what_you_can_see(client):
    """首页说「今天 2 篇新论文」，点进去只有 1 篇——那个数字在说别人的事。"""
    from app.services import buddy

    _owner, _member, _mine = await _two_users_with_different_fields(client)

    from sqlalchemy import select

    from app.models.user import User

    async with get_sessionmaker()() as session:
        member_row = (
            await session.execute(select(User).where(User.email == "member@example.com"))
        ).scalar_one()
        stats = await buddy.collect_stats(session, user_id=member_row.id)
    assert stats.daily_today == 1


async def test_the_agent_tool_browses_your_slice_not_the_pool(client):
    """agent 工具此前只传 user_id 不传 user，过滤被静默跳过——它替这个人翻了整池。"""
    from sqlalchemy import select

    from app.models.user import User
    from app.tools import libraries as library_tools

    _owner, _member, _mine = await _two_users_with_different_fields(client)

    async with get_sessionmaker()() as session:
        member_row = (
            await session.execute(select(User).where(User.email == "member@example.com"))
        ).scalar_one()
        member_id = member_row.id

    class _Ctx:
        user_id = member_id
        project_id = None

    result = await library_tools.search_daily_pool(_Ctx(), {"limit": 50})
    titles = [p["title"] for p in result["papers"]]
    assert titles == ["Cortical circuits"]
