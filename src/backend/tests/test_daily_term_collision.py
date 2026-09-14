"""两个源订了同一个词（#773）。

订阅词不是天然全局唯一的：arXiv 的分类名恰好是，但按关键词取的源订成
``machine learning`` 就会和任何同词订阅撞上。以前是后跑的源直接顶掉先跑的，那个源
当天的论文一篇不进池，而每一步都报成功——每日池是所有文献库的唯一供给，公告只出现
一次，这种丢失补不回来。
"""

import datetime as dt

import pytest

from app.core.db import get_sessionmaker
from app.services import daily_feed
from app.services.daily_feed import Subscription, _merge_status
from app.services.literature import sources as literature_sources


class _Stub:
    """一个最小的日更源：给定词 → 条目，外加它自己声明的批次日期。"""

    def __init__(self, by_term, batch_date=None, fail=False):
        self._by_term = by_term
        self._batch_date = batch_date
        self._fail = fail

    async def fetch_new(self, term):
        if self._fail:
            raise RuntimeError("upstream down")
        at = (
            dt.datetime.combine(self._batch_date, dt.time(4, tzinfo=dt.UTC))
            if self._batch_date
            else None
        )
        return list(self._by_term.get(term, [])), at


def _entry(title):
    return {"title": title, "arxiv_id": None, "doi": f"10.1234/{title}"}


@pytest.fixture
def two_sources(monkeypatch):
    """把两个源都接上 fetch_new 能力，替掉注册表探测。"""

    def install(first, second):
        monkeypatch.setattr(
            literature_sources,
            "sources_with_capability",
            lambda capability, clients=None: [("keyword", first), ("biorxiv", second)],
        )

    return install


async def _subscribe_both(term):
    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session,
            [
                Subscription(source="keyword", terms=(term,)),
                Subscription(source="biorxiv", terms=(term,)),
            ],
        )
        await session.commit()


# ---- 合并本身 ----


def test_a_single_source_is_written_through_unchanged():
    """只有一个源供这个词时逐字节不变——今天所有部署都是这种情形。"""
    statuses = {}
    state = {"count": 3, "status": "ok", "detail": None, "batch_date": "2026-09-13"}
    _merge_status(statuses, "cs.AI", state)
    assert statuses["cs.AI"] is state


def test_counts_add_up_instead_of_the_last_source_winning():
    statuses = {}
    _merge_status(statuses, "ml", {"count": 3, "status": "ok", "detail": None})
    _merge_status(statuses, "ml", {"count": 4, "status": "ok", "detail": None})
    assert statuses["ml"]["count"] == 7


def test_one_source_failing_makes_the_whole_term_an_error():
    """部分残缺也是残缺：这个词今天的论文会缺一块，静默残缺比整体失败更危险。"""
    statuses = {}
    _merge_status(statuses, "ml", {"count": 9, "status": "ok", "detail": None})
    _merge_status(statuses, "ml", {"count": 0, "status": "error", "detail": "boom"})
    assert statuses["ml"]["status"] == "error"
    assert "boom" in statuses["ml"]["detail"]


def test_the_earliest_batch_date_wins_so_a_lagging_source_still_trips_stale():
    """取最新会把落后的那批也标成当天，等于把「抓早了」这个故障重新藏起来。"""
    statuses = {}
    _merge_status(
        statuses, "ml", {"count": 1, "status": "ok", "batch_date": "2026-09-13", "stale": False}
    )
    _merge_status(
        statuses, "ml", {"count": 1, "status": "ok", "batch_date": "2026-09-12", "stale": True}
    )
    assert statuses["ml"]["batch_date"] == "2026-09-12"
    assert statuses["ml"]["stale"] is True


# ---- 端到端：真走一遍抓取 ----


async def test_both_sources_reach_the_pool_when_they_share_a_term(client, two_sources):
    two_sources(
        _Stub({"machine learning": [_entry("from-keyword")]}),
        _Stub({"machine learning": [_entry("from-biorxiv")]}),
    )
    await _subscribe_both("machine learning")

    async with get_sessionmaker()() as session:
        categories, by_term, statuses = await daily_feed.fetch_new_by_category(session)

    titles = {e["title"] for e in by_term["machine learning"]}
    assert titles == {"from-keyword", "from-biorxiv"}, "一个源的当天结果被另一个顶掉了"
    assert statuses["machine learning"]["count"] == 2
    # 词只登记一次：分类列表是给用户看的，同一个词出现两遍只是噪音
    assert categories == ["machine learning"]


async def test_a_broken_source_does_not_take_the_other_ones_papers_with_it(
    client, two_sources
):
    two_sources(
        _Stub({"machine learning": [_entry("survivor")]}),
        _Stub({}, fail=True),
    )
    await _subscribe_both("machine learning")

    async with get_sessionmaker()() as session:
        _categories, by_term, statuses = await daily_feed.fetch_new_by_category(session)

    # 抓到的那些照常带走，同时如实报错——两件事都要成立
    assert [e["title"] for e in by_term["machine learning"]] == ["survivor"]
    assert statuses["machine learning"]["status"] == "error"
    assert statuses["machine learning"]["count"] == 1
