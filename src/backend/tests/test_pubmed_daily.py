"""PubMed 供每日池（#778）。

每日池的管道早就源无关了，可是**只有 arXiv 实现了 fetch_new**——所以对做生物、
做医学的人，池子照样是空的。这里的用例盯两件事：能力确实只挂在 PubMed 上（别的
多源提供方不该跟着声称能日更），以及日更查的是「进库日」而不是「出版日」。
"""

import datetime as dt

import pytest

from app.services import daily_feed
from app.services.daily_feed import Subscription
from app.services.literature import sources as literature_sources
from app.services.literature.multi_source import (
    PUBMED_DAILY_WINDOW_DAYS,
    MultiSourceClient,
)
from tests.conftest import owner_of, register_and_login


@pytest.fixture
def captured_query(monkeypatch):
    """截下发给 esearch 的 term 与排序，不打网络。"""
    seen: dict[str, object] = {}

    async def fake_rows(self, query, *, limit, sort):
        seen["query"] = query
        seen["limit"] = limit
        seen["sort"] = sort
        return [
            {
                "source": "pubmed",
                "pmid": "40000001",
                "title": "Hippocampal circuits under chronic stress",
                "doi": "10.1000/pubmed.1",
                "authors": ["Li Wei"],
                "year": 2026,
                "abstract": "A rolling-indexed record.",
            }
        ]

    monkeypatch.setattr(MultiSourceClient, "_pubmed_rows", fake_rows)
    return seen


# ---- 能力挂在谁身上 ----


def test_only_pubmed_among_the_multi_source_providers_can_do_daily():
    """能力探测走 hasattr。加在共用基类上等于 crossref、hal、core 一起声称能日更，
    然后每天在每个订阅词上失败一次。"""
    capable = dict(literature_sources.sources_with_capability("fetch_new"))
    assert "pubmed" in capable
    for other in ("crossref", "europepmc", "hal", "core", "base", "sciverse"):
        assert other not in capable, other


def test_arxiv_still_supplies_the_daily_pool():
    """加一个源不该把原来那个挤掉。"""
    capable = dict(literature_sources.sources_with_capability("fetch_new"))
    assert "arxiv" in capable


def test_pubmed_keeps_its_place_in_the_source_order():
    """注册顺序即可选源清单的顺序；单独注册 pubmed 不该把它挪位置。"""
    ids = [sid for sid, _ in literature_sources.sources_with_capability("search")]
    assert ids.index("arxiv") < ids.index("pubmed") < ids.index("crossref")


# ---- 查的是进库日，不是出版日 ----


async def test_the_daily_query_uses_the_entrez_date_not_the_publication_date(
    captured_query,
):
    """一篇 2025 年 12 月出版的文章可能 2026 年 3 月才进 PubMed。按出版日查「今天」
    会把它永远漏掉；进库日才是 arXiv「今天公告了什么」的对应物。"""
    await MultiSourceClient().fetch_new_pubmed("neuroscience")
    query = captured_query["query"]
    assert "[edat]" in query
    assert "[pdat]" not in query
    assert "neuroscience" in query


async def test_the_window_looks_back_more_than_a_day(captured_query):
    """PubMed 全天滚动收录：一天的窗口在凌晨跑时几乎总是空的，而漏掉的那天
    补不回来（每日池是所有文献库的唯一供给）。"""
    await MultiSourceClient().fetch_new_pubmed("oncology")
    query = str(captured_query["query"])
    today = dt.datetime.now(dt.UTC).date()
    since = today - dt.timedelta(days=PUBMED_DAILY_WINDOW_DAYS - 1)
    assert f"{since:%Y/%m/%d}:{today:%Y/%m/%d}" in query
    assert PUBMED_DAILY_WINDOW_DAYS >= 2


async def test_new_records_are_sorted_by_date_not_relevance(captured_query):
    """日更要的是「最近进来的」，按相关性排会让窗口内的旧记录挤掉新记录。"""
    await MultiSourceClient().fetch_new_pubmed("immunology")
    assert captured_query["sort"] == "date"


async def test_pubmed_declares_no_batch_date(captured_query):
    """滚动收录的源没有「今天这批」，报一个日期就是编的——调用方据此跳过
    「抓早了」的判断，而那个判断对这种源本来就不成立。"""
    adapter = literature_sources.get_source("pubmed")
    entries, batch_at = await adapter.fetch_new("neuroscience")
    assert batch_at is None
    assert entries and entries[0]["doi"] == "10.1000/pubmed.1"


# ---- 端到端：条目真的进池 ----


async def test_a_pubmed_entry_reaches_the_daily_pool(client, captured_query):
    """走完整条路——订阅 → 抓取 → 落库。只断言「抓回来了」的话，非 arXiv 的条目
    会在两步之后被 upsert 静默丢掉，而那正是这条线上犯过的错。"""
    # 订阅按人存（#806）：得先有一个人，才谈得上订阅和信息流
    await register_and_login(client)
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry

    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session,
            [Subscription(source="pubmed", terms=("neuroscience",))],
            user=await owner_of(session),
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        categories, by_term, statuses = await daily_feed.fetch_new_by_category(session)
        assert categories == ["neuroscience"]
        assert statuses["neuroscience"]["status"] == "ok"
        await daily_feed.upsert_entries(session, by_category=by_term)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(DailyFeedEntry))).scalars().all()
    assert len(rows) == 1, "只给 DOI、不给 arxiv_id 的条目以前会在这一步被丢掉"
    assert rows[0].primary_category == "neuroscience"
    # 记下是 PubMed 带来的：以前池论文一律标成 arxiv，库的增量同步分不出来源（#821）
    assert rows[0].sources == ["pubmed"]


# ---- 池条目的来源与标识（#821） ----


async def _pool_rows():
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry
    from app.models.paper import Paper

    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(DailyFeedEntry, Paper).join(Paper, Paper.id == DailyFeedEntry.paper_id)
            )
        ).all()


async def test_pool_papers_record_the_source_that_brought_them(client):
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await daily_feed.upsert_entries(
            session,
            by_category={
                "neuro": [
                    {"title": "PubMed one", "doi": "10.1/p1", "pmid": "111", "_source": "pubmed"}
                ],
                "cs.AI": [{"title": "arXiv one", "arxiv_id": "2609.00001", "_source": "arxiv"}],
            },
        )
    by_title = {paper.title: (entry, paper) for entry, paper in await _pool_rows()}
    entry, paper = by_title["PubMed one"]
    assert entry.sources == ["pubmed"]
    assert paper.source == "pubmed"
    assert paper.external_ids == {"doi": "10.1/p1", "pmid": "111"}
    entry, paper = by_title["arXiv one"]
    assert entry.sources == ["arxiv"]
    assert paper.source == "arxiv"
    assert paper.external_ids == {"arxiv": "2609.00001"}


async def test_a_pubmed_record_without_a_doi_is_kept_by_its_pmid(client):
    """刚收录的 PubMed 记录常常还没有 DOI；以前没有 arXiv 号也没有 DOI 就整条丢掉。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await daily_feed.upsert_entries(
            session,
            by_category={"neuro": [{"title": "Fresh record", "pmid": "222", "_source": "pubmed"}]},
        )
    rows = await _pool_rows()
    assert len(rows) == 1
    entry, paper = rows[0]
    assert paper.external_ids == {"pmid": "222"}
    assert entry.sources == ["pubmed"]


async def test_a_paper_brought_by_two_sources_records_both(client):
    """同一篇被 arXiv 和 PubMed 都带来：选了任一个源的库都该看得到它。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await daily_feed.upsert_entries(
            session,
            by_category={
                "q-bio.NC": [
                    {
                        "title": "Shared",
                        "doi": "10.1/shared",
                        "arxiv_id": "2609.00002",
                        "_source": "arxiv",
                    }
                ],
                "neuro": [
                    {"title": "Shared", "doi": "10.1/shared", "pmid": "333", "_source": "pubmed"}
                ],
            },
        )
    rows = await _pool_rows()
    assert len(rows) == 1
    assert sorted(rows[0][0].sources) == ["arxiv", "pubmed"]
