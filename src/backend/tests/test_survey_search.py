# ruff: noqa: F811 — 从 test_research_runs 借来的夹具在用例参数里重名是有意的
"""调研检索与文献来源的健壮性。

- 来源记录的日期、作者写得怪，不让整个来源失败（Crossref 的 date-parts）；
- 哪些来源现在能用（没配密钥的不推荐）；没有密钥的 Semantic Scholar 只试一次；
- 调研检索：每条查询按相关度各查一次、跨来源去重融合、近期份额、来源失败或超时时
  由别的来源补上、沿引用关系扩展；
- 建库检索（旧路径）：出网之前不留写事务；失败来源的名额由别的来源补；
- 留下但没写解读的论文也有摘要索引；
- 本地库正忙：归为临时故障，给人看的那句话里没有 SQL；
- 三条研究同时跑，写锁不被长时间占住。
"""

import asyncio
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
import respx
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents.voyage import actions_wiki
from app.agents.voyage.failures import classify_failure, describe_failure
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.schemas.literature_discovery import SourceSearchRequest
from app.services import research_runs as research
from app.services import voyages as voyages_service
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
    survey_search,
)
from app.services.literature import sources as literature_sources
from app.services.literature.multi_source import MultiSourceClient, date_parts_iso
from app.services.literature.openalex import _simplify
from app.services.literature.sources import MultiSourceAdapter, SemanticScholarAdapter
from app.services.relevance import RelevanceOut, score_paper_relevance
from app.services.research_runs import Facts, classify_survey
from tests.test_research_runs import (  # noqa: F401 — fixtures
    SIX_RELEVANT,
    _feed,
    _login,
    literature,
    live_queue,
)

NOW = datetime(2026, 11, 1, tzinfo=UTC)

# ---- 来源记录写得怪 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"date-parts": [[2024]]}, "2024-01-01"),
        ({"date-parts": [[2024, 5]]}, "2024-05-01"),
        ({"date-parts": [[2024, 5, 17]]}, "2024-05-17"),
        ({"date-parts": [["2024", "05"]]}, "2024-05-01"),
        ({"date-parts": [[None]]}, None),
        ({"date-parts": None}, None),
        ({"date-parts": [[2024, 13, 40]]}, "2024-01-01"),
        (None, None),
        ("2024", None),
        (2024, None),
    ],
)
def test_date_parts_of_any_shape_never_raise(raw, expected):
    assert date_parts_iso(raw) == expected


@pytest.mark.parametrize(
    "value", [None, 2024, 3.5, {"date-parts": [[2024, 5]]}, [2024], {"x": 1}, "not a date", ""]
)
def test_parse_iso_accepts_anything(value):
    parsed = actions_wiki._parse_iso(value)  # 以前对非字符串抛 TypeError: fromisoformat
    assert parsed is None or parsed.tzinfo is not None
    assert actions_wiki._parse_iso("2024-05").year == 2024
    assert actions_wiki._parse_iso("2024-05-17T00:00:00Z").day == 17


CROSSREF_ITEMS = [
    {  # 真实记录的样子：published 是 date-parts 对象
        "DOI": "10.1/a",
        "title": ["Joint training of retriever and generator"],
        "published": {"date-parts": [[2024, 5]]},
        "issued": {"date-parts": [[2024, 5]]},
        "author": [{"given": "A", "family": "B"}, "not-an-object", {"name": "Consortium"}],
        "is-referenced-by-count": "12",
    },
    {"DOI": "10.1/b", "title": ["Only a year"], "published-print": {"date-parts": [[2023]]}},
    {"DOI": "10.1/c", "title": ["Null date"], "issued": {"date-parts": [[None]]}, "author": None},
    {"DOI": "10.1/d", "title": ["No date at all"], "link": "oops", "published": "2022"},
    "not even an object",
]


async def test_crossref_records_with_odd_dates_all_come_through():
    client = MultiSourceClient(client=httpx.AsyncClient())

    async def fake_json(*_args, **kwargs):
        assert kwargs["params"]["query.bibliographic"] == "joint training"
        assert kwargs["params"]["sort"] == "relevance"
        return {"message": {"items": CROSSREF_ITEMS}}

    client._request_json = fake_json  # type: ignore[method-assign]
    page = await MultiSourceAdapter("crossref", client).search(
        SourceSearchRequest(query="joint training", limit=10)
    )
    assert [c.doi for c in page.items] == ["10.1/a", "10.1/b", "10.1/c", "10.1/d"]
    assert [c.year for c in page.items] == [2024, 2023, None, None]
    assert page.items[0].citation_count == 12
    assert [a["name"] for a in page.items[0].authors] == ["A B", "Consortium"]
    # 入库用的条目：发表日期解析不抛错（以前这里是 TypeError，Crossref 每次都整个失败）
    for item in page.items:
        entry = actions_wiki._entry_from_candidate(item)
        actions_wiki._parse_iso(entry["published"])
    first = actions_wiki._entry_from_candidate(page.items[0])
    assert actions_wiki._parse_iso(first["published"]) == datetime(2024, 5, 1, tzinfo=UTC)
    await client.aclose()


def test_openalex_record_with_nulls_is_still_usable():
    row = _simplify(
        {
            "id": "https://openalex.org/W1",
            "title": "T",
            "doi": None,
            "primary_location": None,
            "authorships": [{"author": None}, None, {"author": {"display_name": "X"}}],
            "ids": None,
            "publication_year": "2024",
            "cited_by_count": None,
            "locations": [{"landing_page_url": "https://arxiv.org/abs/2005.11401v4"}],
        }
    )
    assert row["authors"] == [{"name": "X", "affiliations": []}]
    assert row["year"] is None and row["cited_by_count"] == 0
    assert row["arxiv_id"] == "2005.11401"


# ---- 哪些来源能用 ----


def test_sources_without_a_key_are_not_proposed():
    usable = literature_sources.usable_search_sources({})
    assert "openalex" in usable and "arxiv" in usable and "crossref" in usable
    assert "semantic" not in usable and "core" not in usable and "sciverse" not in usable
    keyed = literature_sources.usable_search_sources({"provider_keys": {"semantic": ["k"]}})
    assert "semantic" in keyed and "core" not in keyed
    # 设置里登记了但清空 = 停用，不回落到环境变量
    assert not literature_sources.source_has_key("semantic", {"provider_keys": {"semantic": []}})


async def test_brief_only_proposes_usable_sources(app):
    async with get_sessionmaker()() as session:
        draft = {
            "title": "t",
            "statement": "s",
            "include_keywords": ["joint training"],
            "sources": ["semantic", "core", "openalex", "arxiv"],
        }
        brief = await research.finalize_brief(session, "d", draft, {})
        assert brief.sources == ["openalex", "arxiv"]
        # 模型只推荐了不能用的：退回默认里能用的那些，绝不留空
        brief = await research.finalize_brief(session, "d", draft | {"sources": ["core"]}, {})
        assert brief.sources and "semantic" not in brief.sources and "core" not in brief.sources
        # 用户自己点名的照用，没密钥的排最后
        brief = await research.finalize_brief(
            session, "d", draft, {"sources": ["semantic", "openalex"]}
        )
        assert brief.sources == ["openalex", "semantic"]
        assert brief.relevance_threshold == 0.5


async def test_semantic_scholar_without_a_key_gives_up_after_one_attempt():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=5.0)
    with respx.mock() as router:
        route = router.get(url__regex=r"https://api\.semanticscholar\.org/.*").mock(
            return_value=httpx.Response(429)
        )
        started = time.monotonic()
        with pytest.raises(httpx.HTTPStatusError):
            await SemanticScholarAdapter(client).search(SourceSearchRequest(query="q"))
        assert route.call_count == 1  # 以前四次，中间退避 5+10+20+40 秒
        assert time.monotonic() - started < 2
    await client.aclose()
    await redis.aclose()


# ---- 调研检索：查询、融合、取前 N ----


def test_arxiv_expression_phrases_short_queries_and_ands_long_ones():
    assert (
        survey_search.arxiv_expression("retrieval-augmented generation")
        == 'ti:"retrieval-augmented generation" OR abs:"retrieval-augmented generation"'
    )
    long = survey_search.arxiv_expression(
        "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
    )
    assert long == (
        "all:Retrieval-Augmented AND all:Generation AND all:Knowledge-Intensive"
        " AND all:NLP AND all:Tasks"
    )


def test_merge_queries_drops_duplicates_cjk_and_operators():
    assert survey_search.merge_queries(
        ['"RA-DIT" AND dual instruction tuning', "ra dit dual instruction tuning", "中文查询"],
        ["Atlas few-shot", ""],
    ) == ["RA-DIT dual instruction tuning", "Atlas few-shot"]


def _e(title, *, doi=None, arxiv=None, year=2021, cites=0, published=None, abstract=""):
    return {
        "title": title,
        "doi": doi,
        "arxiv_id": arxiv,
        "year": year,
        "citations": cites,
        "published": published,
        "abstract": abstract,
    }


def test_fuse_merges_across_sources_and_ranks_by_agreement():
    rag_arxiv = _e("Retrieval-Augmented Generation for NLP", arxiv="2005.11401v2", year=2020)
    rag_oa = _e(
        "Retrieval-Augmented Generation for NLP",
        doi="10.48550/arXiv.2005.11401",
        cites=5000,
        abstract="long abstract",
    )
    rag_published = _e("Retrieval augmented generation for NLP.", doi="10.5555/neurips.1")
    lists = [
        ("arxiv", [_e("Only on arXiv, top hit", arxiv="2401.00001"), rag_arxiv]),
        ("openalex", [_e("Unrelated hotel matching", doi="10.1/h"), rag_oa]),
        ("crossref", [_e("Something else", doi="10.1/x"), rag_published]),
        ("openalex", [_e("Excluded survey of basket weaving", doi="10.1/bw")]),
    ]
    fused = survey_search.fuse(
        lists, recent_since=datetime(2025, 1, 1, tzinfo=UTC), exclude=["basket weaving"]
    )
    titles = [c.entry["title"] for c in fused]
    assert "Excluded survey of basket weaving" not in titles
    assert len(fused) == 4  # 三个来源各给了一份的那篇合成一条
    top = fused[0]  # 三份结果里都排第二，胜过只在一份里排第一的
    assert top.entry["title"] == "Retrieval-Augmented Generation for NLP"
    assert top.sources == {"arxiv", "openalex", "crossref"}
    assert top.entry["arxiv_id"] == "2005.11401v2" and top.entry["abstract"] == "long abstract"
    assert top.citations == 5000 and top.recent is False


def test_select_keeps_a_share_for_recent_work_but_fills_up_when_there_is_none():
    old = [
        survey_search.Candidate(entry=_e(f"old {i}"), score=10 - i * 0.1, recent=False)
        for i in range(20)
    ]
    new = [
        survey_search.Candidate(entry=_e(f"new {i}"), score=1 - i * 0.1, recent=True)
        for i in range(5)
    ]
    picked = survey_search.select(old + new, 10)
    assert [c.entry["title"] for c in picked[:7]] == [f"old {i}" for i in range(7)]
    assert [c.entry["title"] for c in picked[7:]] == ["new 0", "new 1", "new 2"]
    assert len(survey_search.select(old, 10)) == 10  # 没有近期的：按分数补满
    assert len(survey_search.select(old[:4], 10)) == 4


class _Source:
    """按查询回固定结果的来源替身（通用 search 接口）。"""

    def __init__(self, name, *, n=30, error=None, hang=False):
        self.name, self.n, self.error, self.hang = name, n, error, hang
        self.requests: list[SourceSearchRequest] = []

    async def search(self, request):
        self.requests.append(request)
        if self.hang:
            await asyncio.sleep(30)
        if self.error is not None:
            raise self.error
        items = [
            SimpleNamespace(
                title=f"{self.name} {request.query} paper {i}",
                doi=f"10.9/{self.name}.{abs(hash(request.query)) % 997}.{i}",
                authors=[],
                abstract="a",
                year=2022,
                venue="V",
                url=None,
                metadata={},
            )
            for i in range(min(self.n, request.limit))
        ]
        return SimpleNamespace(items=items)


async def test_collect_fills_the_limit_when_sources_fail_or_stall():
    """三个来源坏了两个（一个报错、一个卡住）：剩下那个把篇数补满，各来源的数如实。"""
    good = _Source("pubmed")
    adapters = {
        "pubmed": good,
        "crossref": _Source("crossref", error=httpx.ConnectError("down")),
        "europepmc": _Source("europepmc", hang=True),
        "core": None,
    }
    started = time.monotonic()
    result = await survey_search.collect(
        adapters, ["q one", "q two", "q three"], limit=30, months_back=36, now=NOW,
        time_budget_s=0.3,
    )  # fmt: skip
    assert time.monotonic() - started < 3  # 卡住的来源到点就放弃，不拖住整次检索
    assert len(result.selected) == 30  # 以前：每个来源各占三分之一，只进来 10 篇
    assert result.fetched == {"pubmed": 60, "crossref": 0, "europepmc": 0, "core": 0}
    assert result.selected_by_source == {"pubmed": 30}
    errors = {e["source"]: e["error"] for e in result.errors}
    assert "ConnectError" in errors["crossref"] and "timed out" in errors["europepmc"]
    assert errors["core"] == "unavailable"
    assert len(_Source("x").requests) == 0 and len(adapters["crossref"].requests) == 2
    # 每条查询各发一次、按相关度、放到近十年
    assert [r.query for r in good.requests] == ["q one", "q two", "q three"]
    assert {r.start_year for r in good.requests} == {2016}
    assert [log.as_dict() for log in result.logs if log.source == "pubmed"][0] == {
        "source": "pubmed",
        "query": "q one",
        "sort": "relevance",
        "window": "2016..2026",
        "returned": 20,
    }


async def test_plan_queries_falls_back_to_keywords_when_the_model_fails():
    class _Down:
        async def complete_json(self, *_a, **_k):
            raise httpx.ConnectError("no model")

    queries, why = await survey_search.plan_queries(
        _Down(), statement="s", questions=[], keywords=["joint training", "RAG"]
    )
    assert queries == ["joint training", "RAG"] and why == "ConnectError"


async def test_citation_expansion_ranks_what_the_relevant_papers_share():
    class _OpenAlex:
        works = {
            "W1": {"openalex_id": "W1", "referenced_works": ["R1", "R2"]},
            "W2": {"openalex_id": "W2", "referenced_works": ["R1", "R3"]},
        }

        async def find_work(self, *, doi=None, arxiv_id=None, title=None):
            return self.works.get(doi)

        async def citing_works(self, openalex_id, *, limit=10):
            if openalex_id == "W1":
                return [{"openalex_id": "C1", "title": "Citing", "cited_by_count": 50}]
            return []

        async def works_by_ids(self, ids):
            cites = {"R1": 9000, "R2": 10, "R3": 400}
            return [
                {"openalex_id": i, "title": f"Ref {i}", "cited_by_count": cites[i], "doi": i}
                for i in ids
            ]

    entries, stats = await survey_search.expand_by_citations(
        _OpenAlex(), [{"doi": "W1"}, {"doi": "W2"}, {"doi": "missing"}], limit=3
    )
    # 两篇种子都引的排最前；其余按被引数
    assert [e["title"] for e in entries] == ["Ref R1", "Ref R3", "Citing"]
    assert stats["seeds"] == 3 and stats["seeds_found"] == 2
    assert stats["references"] == 3 and stats["citing"] == 1 and stats["candidates"] == 3


# ---- 打分：结构化输出 ----


async def test_relevance_uses_structured_output():
    """理由里带引号、换行都不影响：打分走 complete_json，不再从文本里抠 JSON。"""
    seen = {}

    class _LLM:
        async def complete_json(self, stage, messages, *, schema, **kwargs):
            seen.update(stage=stage, schema=schema)
            return SimpleNamespace(
                data=RelevanceOut(score=1.7, reason='讨论了"联合训练"，\n相关', tldr="一句话")
            )

    paper = SimpleNamespace(title="T", abstract="A", tldr=None)
    membership = SimpleNamespace(library_id=uuid.uuid4())
    result = await score_paper_relevance(paper, membership, context_text="方向", llm=_LLM())
    assert seen == {"stage": "relevance", "schema": RelevanceOut}
    assert result.score == 1.0 and '"联合训练"' in membership.relevance_reason
    assert paper.tldr == "一句话"


def test_a_few_unscored_papers_do_not_turn_too_few_relevant_into_a_failure():
    stats = {"source": "openalex", "source_fetched": 120, "selected": 30}
    # 30 篇里 2 篇没打成、留下 1 篇：如实的说法是「相关的太少」
    problem = classify_survey(
        stats, Facts(papers_found=3, papers_kept=1, papers_unscreened=2, papers_indexed=1)
    )
    assert problem["code"] == "SURVEY_TOO_FEW_RELEVANT"
    assert problem["counts"] == {"found": 30, "relevant": 1}
    # 大半没筛：是筛选没做完
    problem = classify_survey(
        stats, Facts(papers_found=22, papers_kept=1, papers_unscreened=21, papers_indexed=1)
    )
    assert problem["code"] == "SURVEY_FAILED"
    # 留下的够了：几篇没筛的不挡路
    assert (
        classify_survey(
            stats, Facts(papers_found=20, papers_kept=12, papers_unscreened=5, papers_indexed=12)
        )
        is None
    )


# ---- 本地库正忙 ----

_LOCKED = OperationalError(
    "UPDATE voyage_runs SET checkpoint=?, updated_at=? WHERE voyage_runs.id = ?",
    ("{" + "x" * 5000 + "}", "2026-11-18", "abc"),
    sqlite3.OperationalError("database is locked"),
)


def test_database_busy_is_transient_with_a_plain_message():
    found = classify_failure(_LOCKED)
    assert (found.code, found.transient) == ("DB_BUSY", True)
    record = describe_failure(_LOCKED, source="action")
    assert record["message"] == "执行步骤时出错：本地数据库正忙"
    assert "database is locked" in record["detail"]
    assert "UPDATE" not in record["detail"] and len(record["detail"]) <= 300
    plan = research._plan_error_record(_LOCKED)
    assert plan["message"] == "本地数据库正忙" and plan["transient"] is True
    assert "UPDATE" not in plan["message"] and len(plan["detail"]) <= 500


# ---- 建库检索（旧路径）：写锁与名额 ----


def _write_lock_is_free() -> bool:
    path = make_url(get_settings().database_url).database
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


class _Probing(_Source):
    """每次被查询时看一眼：此刻别的连接拿不拿得到写锁。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock_free: list[bool] = []

    async def search(self, request):
        self.lock_free.append(_write_lock_is_free())
        return await super().search(request)


async def _inserter(session, name="lib"):
    library = DirectionLibrary(name=name, definition={})
    session.add(library)
    await session.commit()
    return actions_wiki._LibraryInserter(
        session, library.id, arxiv_ids=set(), dois=set(), titles=set()
    )


async def test_library_search_does_not_hold_the_write_lock_across_requests(app, monkeypatch):
    """插完一批就提交：等下一个来源的 HTTP 时不占着 SQLite 的写锁。

    以前只 flush：写事务一直开到所有来源查完，被限流的来源一等几十秒，同时在跑的
    别的任务全卡在写锁上，等过超时就是 database is locked。"""
    stubs = {"pubmed": _Probing("pubmed", n=3), "crossref": _Probing("crossref", n=3)}
    monkeypatch.setattr(literature_sources, "search_adapter", lambda sid, _rt=None: stubs[sid])
    async with get_sessionmaker()() as session:
        inserter = await _inserter(session)
        fetched = await actions_wiki._search_generic_sources(
            inserter,
            source_ids=["pubmed", "crossref"],
            terms=["a", "b"],
            since=NOW,
            until=NOW,
            limit=12,
            errors=[],
        )
    assert fetched == 12 and len(inserter.new_papers) == 12
    assert stubs["pubmed"].lock_free == [True, True]
    assert stubs["crossref"].lock_free == [True, True]


async def test_library_search_gives_a_failed_sources_share_to_the_others(app, monkeypatch):
    stubs = {
        "pubmed": _Source("pubmed", error=httpx.ReadTimeout("slow")),
        "crossref": _Source("crossref", n=100),
        "europepmc": _Source("europepmc", n=2),  # 只有这么多
    }
    monkeypatch.setattr(literature_sources, "search_adapter", lambda sid, _rt=None: stubs[sid])
    errors: list[str] = []
    failures: list[dict] = []
    counts: dict[str, int] = {}
    async with get_sessionmaker()() as session:
        inserter = await _inserter(session)
        fetched = await actions_wiki._search_generic_sources(
            inserter,
            source_ids=["pubmed", "crossref", "europepmc"],
            terms=["only term"],
            since=NOW,
            until=NOW,
            limit=30,
            errors=errors,
            failures=failures,
            counts=counts,
        )
    # 以前：三个来源各 10 篇的名额，一个失败、一个只有 2 篇 → 进来 12 篇
    assert len(inserter.new_papers) == 30
    assert counts == {"crossref": 28, "europepmc": 2} and fetched == 30
    assert [f["source"] for f in failures] == ["pubmed"] and errors == ["pubmed：ReadTimeout"]


# ---- 端到端：摘要索引、三条研究同时跑 ----


async def test_kept_papers_beyond_the_summarised_ones_are_searchable(
    client, live_queue, literature
):
    """留下 6 篇、只给 5 篇写解读：第 6 篇按摘要建了索引，寻找假设时用得上。"""
    headers = await _login(client)
    resp = await client.post(
        "/api/research",
        json={"direction": "agent workflows", "brief": {"compile_top_n": 5}},
        headers=headers,
    )
    await live_queue.drain()
    detail = (await client.get(f"/api/research/{resp.json()['id']}", headers=headers)).json()
    assert detail["phase"] == "choose", detail
    assert detail["survey"]["progress"]["kept"] == 6
    assert detail["survey"]["progress"]["summarised"] == 5
    async with get_sessionmaker()() as session:
        indexed = await voyages_service.indexed_library_paper_count(
            session, uuid.UUID(detail["library_id"])
        )
    assert indexed == 6  # 以前是 5：只有写了解读的才有索引


SOURCE_LATENCY_S = 1.0


async def test_three_runs_at_once_never_keep_the_write_lock_for_long(client, live_queue):
    """三条研究同时跑（文件型 SQLite、来源每次请求要等 1 秒）：别的写入最多等一小会儿。

    量的是「另一条连接拿写锁要等多久」。修之前检索带着写事务去等来源，这个数至少是
    一次请求的时长；等的时间超过 busy_timeout 就是用户看到的 database is locked。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )

    def feed_for(request):
        query = request.url.params.get("search_query", "")
        tag = next((i for i in range(3) if f"topic{i}" in query), 0)
        entries = [
            (f"26{tag:02d}.{i:05d}", f"Verifiable Agent Workflow {tag} Study {i}", "agent planning")
            for i in range(1, 7)
        ]
        return _feed(entries)

    async def slow_arxiv(request):
        await asyncio.sleep(SOURCE_LATENCY_S)
        return httpx.Response(200, text=feed_for(request))

    async def slow_crossref(request):
        await asyncio.sleep(SOURCE_LATENCY_S)
        query = request.url.params.get("query.bibliographic", "")
        items = [
            {
                "DOI": f"10.2/{abs(hash(query)) % 10**6}.{i}",
                "title": [f"Crossref verification workflow {query} {i}"],
                "published": {"date-parts": [[2025, 4]]},
            }
            for i in range(4)
        ]
        return httpx.Response(200, json={"message": {"items": items}})

    waits: list[float] = []
    stop = False

    def wait_for_write_lock() -> float:
        path = make_url(get_settings().database_url).database
        conn = sqlite3.connect(path, timeout=60, isolation_level=None)
        started = time.monotonic()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        return time.monotonic() - started

    async def prober():
        while not stop:
            waits.append(await asyncio.to_thread(wait_for_write_lock))
            await asyncio.sleep(0.02)

    headers = await _login(client)
    with respx.mock(assert_all_called=False) as inner:
        inner.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(side_effect=slow_arxiv)
        inner.get(url__regex=r"https://api\.crossref\.org/works.*").mock(side_effect=slow_crossref)
        inner.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(404))
        inner.route().mock(return_value=httpx.Response(404, json={}))
        probe = asyncio.create_task(prober())
        ids = []
        for i in range(3):
            brief = {
                "title": f"T{i}",
                "statement": "agent workflow verification",
                "include_keywords": [f"topic{i} agent planning"],
                "sources": ["arxiv", "crossref"],
                "max_papers": 20,
                "compile_top_n": 5,
            }
            resp = await client.post(
                "/api/research",
                json={"direction": f"direction {i} agent workflows", "brief": brief},
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
            ids.append(resp.json()["id"])
        await live_queue.drain()
        stop = True
        await probe
    reset_clients()
    await redis.aclose()
    for run_id in ids:
        detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
        assert detail["phase"] == "choose" and detail["last_error"] is None, detail
        assert {s["source"] for s in detail["survey"]["sources"]} == {"arxiv", "crossref"}
    assert len(waits) > 20
    assert max(waits) < SOURCE_LATENCY_S * 0.7, max(waits)
