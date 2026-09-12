"""每日池的源解耦（#753）。

审计判据是「一个做结构工程的人能用吗」：arXiv 上几乎没有土木，所以对他，每日池
不是「相关性差」，是**空的**。而且此前的耦合有四层，只改抓取那一行是装样子——
非 arXiv 的条目会在两步之后被 upsert 静默丢掉。

所以这里的用例刻意**走完整条路**：注册一个假源 → 订阅它 → 抓取 → 落库，
断言条目真的进了池子，而不是只断言「抓回来了」。
"""

import datetime as dt

import pytest

from app.services import daily_feed
from app.services.literature import sources as literature_sources
from app.services.literature.sources import SourceSpec, register_source, unregister_source


class _FakeCivilSource:
    """一个非 arXiv 的源：给 DOI、不给 arxiv_id，正是以前会被丢掉的形状。"""

    name = "civil"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(self, request):  # pragma: no cover - 本用例不走检索
        raise NotImplementedError

    async def fetch_new(self, category: str):
        self.calls.append(category)
        return (
            [
                {
                    "title": f"Blast response of composite beams ({category})",
                    "doi": f"10.1000/civil.{category}",
                    "authors": ["Wei Zhang"],
                    "year": 2026,
                    "abstract": "Impact loading on steel-concrete members.",
                }
            ],
            dt.datetime.now(dt.UTC),
        )


@pytest.fixture
def civil_source():
    fake = _FakeCivilSource()
    register_source(
        SourceSpec(id="civil", build=lambda _ctx: fake, default_factory=lambda _c: fake)
    )
    yield fake
    unregister_source("civil")


def test_capability_probe_finds_sources_that_can_do_daily(civil_source):
    ids = {sid for sid, _ in literature_sources.sources_with_capability("fetch_new")}
    # 谁能干这件事由注册表回答，而不是调用点写死一个 id
    assert "civil" in ids
    assert "arxiv" in ids


def test_a_source_without_the_capability_is_not_offered():
    ids = {sid for sid, _ in literature_sources.sources_with_capability("fetch_new")}
    # openalex 现在没有日更能力（只有年粒度过滤），不该被当成可日更的源
    assert "openalex" not in ids


def test_malformed_subscription_rows_are_skipped_not_fatal():
    """这份配置是人写的，一行写错不该让整个每日池停摆。"""
    subs = daily_feed._normalize_extra(
        [{"source": "civil"}, {"terms": ["x"]}, "junk", {"source": "ok", "terms": ["t"]}]
    )
    assert [s.source for s in subs] == ["ok"]


def test_arxiv_rows_never_come_from_the_new_key():
    """arXiv 的订阅只有一个真相来源（旧键）；新键里混进 arxiv 行要被忽略，
    否则同一个源会有两处配置、且互相看不见。"""
    subs = daily_feed._normalize_extra([{"source": "arxiv", "terms": ["cs.AI"]}])
    assert subs == []


async def test_non_arxiv_terms_are_not_forced_into_arxiv_category_shape(client):
    """自由检索词必须能订：把「structural engineering」按 arXiv 分类格式校验就是把
    非 CS 学科挡在门外。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        saved = await daily_feed.set_subscriptions(
            session,
            [daily_feed.Subscription(source="civil", terms=("structural engineering",))],
        )
    assert saved[0].terms == ("structural engineering",)

    async with get_sessionmaker()() as session:
        # arXiv 的词仍按分类格式校验——它的词确实是分类
        with pytest.raises(daily_feed.InvalidCategoryError):
            await daily_feed.set_subscriptions(
                session,
                [daily_feed.Subscription(source="arxiv", terms=("not a category!",))],
            )


async def test_legacy_key_keeps_its_flat_shape(client):
    """存量部署的 arXiv 订阅仍存在原键、仍是扁平列表——升级不需要迁移数据。"""
    from app.core.db import get_sessionmaker
    from app.services import owner_settings

    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session,
            [
                daily_feed.Subscription(source="arxiv", terms=("cs.AI",)),
                daily_feed.Subscription(source="civil", terms=("blast loading",)),
            ],
        )
    async with get_sessionmaker()() as session:
        stored = await owner_settings.read_setting(
            session, daily_feed.CATEGORIES_USER_KEY, legacy_key=daily_feed.CATEGORIES_SETTING_KEY
        )
    assert stored == ["cs.AI"], "旧键必须还是扁平分类列表，老读者才不会看见不认识的形状"


async def test_arxiv_compat_accessor_preserves_other_sources(client):
    """旧的 set_categories 只动 arXiv 那条，别的源的订阅在另一个键上不受影响。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session,
            [
                daily_feed.Subscription(source="arxiv", terms=("cs.AI",)),
                daily_feed.Subscription(source="civil", terms=("blast loading",)),
            ],
        )
    async with get_sessionmaker()() as session:
        await daily_feed.set_categories(session, ["cs.CV"])
    async with get_sessionmaker()() as session:
        subs = {s.source: s.terms for s in await daily_feed.get_subscriptions(session)}
    assert subs["arxiv"] == ("cs.CV",)
    assert subs["civil"] == ("blast loading",)  # 没被 arXiv 口径的外壳抹掉


async def test_unavailable_source_is_reported_not_silently_skipped(client):
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session, [daily_feed.Subscription(source="ghost", terms=("anything",))]
        )
    async with get_sessionmaker()() as session:
        _cats, _entries, statuses = await daily_feed.fetch_new_by_category(session)
    # 「这个源没装」和「今天没有新论文」必须可区分
    assert statuses["anything"]["status"] == "error"
    assert "ghost" in statuses["anything"]["detail"]


async def test_entry_with_only_a_doi_reaches_the_pool(client, civil_source):
    """这条是整件事的要害：以前非 arXiv 的条目抓回来了，然后在 upsert 被静默丢掉。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry

    async with get_sessionmaker()() as session:
        await daily_feed.set_subscriptions(
            session, [daily_feed.Subscription(source="civil", terms=("blast",))]
        )

    async with get_sessionmaker()() as session:
        categories, by_category, statuses = await daily_feed.fetch_new_by_category(session)
        assert statuses["blast"]["status"] == "ok"
        assert len(by_category["blast"]) == 1
        await daily_feed.upsert_entries(
            session,
            by_category=by_category,
            batch_dates=daily_feed.batch_dates_from_statuses(statuses),
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(DailyFeedEntry))).scalars().all()
    assert len(rows) == 1, "只有 DOI 的条目必须能进池，否则非 arXiv 的源接了也没用"
