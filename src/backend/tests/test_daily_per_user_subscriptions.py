"""每日订阅按人存（#806）。

此前订阅挂在 owner 头上：多人实例上池子里装的是第一个注册者的领域，别人看到的
是他的论文，而唯一能改它的开关一改就是所有人一起改。

池子仍然共享——抓一次、各自排序，比每人抓一遍便宜且天然去重。变的是两件事：
填池子的是**全体订阅的并集**，看到的是**与自己订阅相交**的那部分。

这两件事必须同时成立才有意义：只做并集不做过滤，每多一个用户、每个人的信息流就
多一批与自己无关的论文，比今天「全看 owner 的」更糟；只做过滤不做并集，第二个人
订的词根本没人去抓，他的信息流永远是空的。
"""

import datetime as dt
import uuid

from app.api.auth import LOCAL_USER_EMAIL
from app.core.db import get_sessionmaker
from app.models.daily_feed import DailyFeedEntry
from app.models.paper import Paper
from app.services import daily_feed
from tests.conftest import owner_of, register_and_login


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


async def _auth(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _add_entry(title: str, category: str, *, extra: list[str] | None = None) -> uuid.UUID:
    """往共享池里塞一条，记下把它抓进来的词（categories 就是这个用途）。"""
    async with get_sessionmaker()() as session:
        paper = Paper(source="arxiv", dedup_key=uuid.uuid4().hex, title=title, abstract=title)
        session.add(paper)
        await session.flush()
        session.add(
            DailyFeedEntry(
                paper_id=paper.id,
                feed_date=_today(),
                primary_category=category,
                categories=[category, *(extra or [])],
            )
        )
        await session.commit()
        return paper.id


async def _feed_titles(client, headers) -> list[str]:
    resp = await client.get("/api/daily/papers?size=50", headers=headers)
    assert resp.status_code == 200, resp.text
    return [row["title"] for row in resp.json()["items"]]


# ---------------------------------------------------------------- 各看各的


async def test_a_cross_listed_entry_reaches_a_matching_subscription(client):
    """交叉命中的条目按 categories 里累积的全部词算，不只看主分类。

    只看 primary_category 的话，一篇因为交叉列表进池的论文会在订了那个交叉词的人
    那里凭空消失。
    """
    me = await _auth(client, "me@example.com")
    await client.put("/api/daily/categories", json={"categories": ["stat.ML"]}, headers=me)

    await _add_entry("Shared work", "cs.AI", extra=["stat.ML"])

    assert await _feed_titles(client, me) == ["Shared work"]


async def test_no_subscription_means_an_empty_feed(client):
    """没订阅 = 什么都不给看，哪怕池子里有论文。界面对空订阅另有「先去订阅」的提示。"""
    me = await _auth(client, "me@example.com")
    await _add_entry("Agent planning", "cs.AI")

    assert await _feed_titles(client, me) == []


# ---------------------------------------------------------------- 共享池仍然共享


async def test_the_union_merges_the_same_term_once(client):
    """同一个词只抓一次。"""
    a = await _auth(client, "a@example.com")
    await client.put("/api/daily/categories", json={"categories": ["cs.AI", "cs.AI"]}, headers=a)

    async with get_sessionmaker()() as session:
        union = await daily_feed.all_subscriptions(session)
    arxiv = next(s for s in union if s.source == "arxiv")
    assert arxiv.terms == ("cs.AI",)


async def test_the_union_spans_sources(client):
    me = await _auth(client, "a@example.com")
    resp = await client.put(
        "/api/daily/subscriptions",
        json={
            "subscriptions": [
                {"source": "arxiv", "terms": ["cs.AI"]},
                {"source": "pubmed", "terms": ["glioma"]},
            ]
        },
        headers=me,
    )
    assert resp.status_code == 200, resp.text

    async with get_sessionmaker()() as session:
        union = {s.source: s.terms for s in await daily_feed.all_subscriptions(session)}
    assert union["arxiv"] == ("cs.AI",)
    assert union["pubmed"] == ("glioma",)


async def test_a_deactivated_user_stops_costing_fetches(client):
    """停用的账号不该继续让平台每天替他抓。"""
    me = await _auth(client, "a@example.com")
    await client.put("/api/daily/categories", json={"categories": ["cs.AI"]}, headers=me)

    from sqlalchemy import select

    from app.models.user import User

    async with get_sessionmaker()() as session:
        gone = (
            await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
        ).scalar_one()
        gone.is_active = False
        await session.commit()

    async with get_sessionmaker()() as session:
        union = {s.source: s.terms for s in await daily_feed.all_subscriptions(session)}
    assert "arxiv" not in union


# ---------------------------------------------------------------- 单人部署不变


async def test_a_single_user_deployment_behaves_as_before(client):
    """唯一那个用户既是 owner 也是读者：订什么就看到什么，与改动之前一致。"""
    only = await _auth(client, "solo@example.com")
    await client.put("/api/daily/categories", json={"categories": ["cs.AI", "cs.CL"]}, headers=only)
    await _add_entry("Agent planning", "cs.AI")
    await _add_entry("Parsing", "cs.CL")
    await _add_entry("Somebody else's field", "q-bio.NC")

    titles = await _feed_titles(client, only)
    assert sorted(titles) == ["Agent planning", "Parsing"]

    async with get_sessionmaker()() as session:
        owner = await owner_of(session)
        assert await daily_feed.get_categories(session, owner) == ["cs.AI", "cs.CL"]
