"""SQLite 上的语义检索：候选在 SQL 里圈、余弦在 Python 侧算（services/vector_search.py）。

每个检索入口都要验证同一件事：排序按余弦降序，且候选语句里的每一个过滤
条件在这里同样生效——库外的、回收站/候选状态的、别的向量空间的、维度不对的，一律不出现。
测试库就是 SQLite，所以这些是真正跑过检索的端到端用例，而不是源码级守卫。
"""

import datetime as dt
import math
import uuid

import pytest
from sqlalchemy import JSON, Column, Integer, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base

from app.core.db import get_sessionmaker
from app.core.embedding_space import EmbeddingSpace
from app.models.daily_feed import DailyFeedEntry
from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import PaperChunk, new_paper
from app.models.paper_assets import AssetGrant
from app.models.paper_content import PaperContentChunk, PaperContentChunkVector
from app.models.vectors import PaperChunkVector, PaperVector
from app.services import chunks as chunks_service
from app.services import daily_feed as daily_service
from app.services import papers as papers_service
from app.services import user_library as user_library_service
from app.services import vector_search
from app.services.evidence import semantic_search_current_fulltext
from app.services.libraries import get_source_library_ids
from tests.conftest import add_paper, register_and_login

pytestmark = pytest.mark.asyncio

SPACE = EmbeddingSpace(model="vec-test", dim=3)
OTHER_SPACE = EmbeddingSpace(model="vec-other", dim=3)
QUERY = [1.0, 0.0, 0.0]
# 与 QUERY 的余弦：BEST=1.0 > GOOD≈0.894 > FAR≈0.447 > OPPOSITE=-1
BEST = [2.0, 0.0, 0.0]
GOOD = [2.0, 1.0, 0.0]
FAR = [1.0, 2.0, 0.0]
OPPOSITE = [-1.0, 0.0, 0.0]


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---- 打分工具本身 ----


async def test_top_k_orders_by_cosine_and_truncates():
    rows = [("far", FAR), ("best", BEST), ("good", GOOD), ("opp", OPPOSITE)]
    hits = vector_search.top_k_cosine(QUERY, rows, 3)
    assert [key for key, _ in hits] == ["best", "good", "far"]
    for key, score in hits:
        assert score == pytest.approx(_cos(QUERY, dict(rows)[key]))


async def test_top_k_skips_bad_rows_instead_of_crashing():
    rows = [
        ("short", [1.0, 0.0]),  # 维度不对
        ("long", [1.0, 0.0, 0.0, 0.0]),
        ("broken-json", "[1.0, 0.0,"),
        ("not-a-list", {"a": 1}),
        ("none", None),
        ("non-numeric", [1.0, "x", 0.0]),
        ("nan", [float("nan"), 0.0, 0.0]),
        ("zero", [0.0, 0.0, 0.0]),  # pgvector 给 NaN，排不出位置
        ("json-text", "[2.0, 1.0, 0.0]"),  # 未解码的 JSON 文本照样能用
        ("ok", FAR),
    ]
    hits = vector_search.top_k_cosine(QUERY, rows, 10)
    assert [key for key, _ in hits] == ["json-text", "ok"]


async def test_raw_json_text_rows_parse_like_decoded_lists():
    """SQLite 上向量列按原始文本取出（raw_embedding），numpy 直接解析；坏文本同样跳过。"""
    rows = [
        ("good", "[2.0, 1.0, 0.0]"),
        ("spaced", "  [1e0,2,\n0]  "),
        ("truncated", "[1.0, 2.0"),
        ("garbage", "[1.0, abc, 0.0]"),
        ("short", "[1.0, 0.0]"),
        ("empty", "[]"),
        ("bytes", b"[2.0, 0.0, 0.0]"),
    ]
    hits = vector_search.top_k_cosine(QUERY, rows, 10)
    assert [key for key, _ in hits] == ["bytes", "good", "spaced"]
    assert hits[1][1] == pytest.approx(_cos(QUERY, GOOD))


async def test_top_k_empty_and_degenerate_inputs():
    assert vector_search.top_k_cosine(QUERY, [], 5) == []
    assert vector_search.top_k_cosine(QUERY, [("a", BEST)], 0) == []
    assert vector_search.top_k_cosine([0.0, 0.0, 0.0], [("a", BEST)], 5) == []
    assert vector_search.top_k_cosine([], [("a", BEST)], 5) == []


async def test_top_k_counts_a_key_once_and_keeps_ties_stable():
    rows = [("a", GOOD), ("b", GOOD), ("a", GOOD), ("c", BEST)]
    hits = vector_search.top_k_cosine(QUERY, rows, 10)
    assert [key for key, _ in hits] == ["c", "a", "b"]


async def test_top_k_streams_across_batches():
    top = vector_search.TopK(QUERY, 2)
    top.add([("far", FAR), ("good", GOOD)])
    top.add([("best", BEST), ("opp", OPPOSITE)])
    assert [key for key, _ in top.result()] == ["best", "good"]


async def test_pure_python_fallback_matches_numpy(monkeypatch):
    rows = [(i, [math.sin(i), math.cos(i), (i % 7) - 3.0]) for i in range(200)]
    rows.append((999, [1.0, 2.0]))  # 坏行两条路径都要跳过
    with_numpy = vector_search.top_k_cosine(QUERY, rows, 15)
    monkeypatch.setattr(vector_search, "np", None)
    without = vector_search.top_k_cosine(QUERY, rows, 15)
    assert [k for k, _ in with_numpy] == [k for k, _ in without]
    for (_, a), (_, b) in zip(with_numpy, without, strict=True):
        assert a == pytest.approx(b)


# ---- 造数据 ----


async def _project(client, headers, name):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return uuid.UUID(body["id"]), uuid.UUID(body["owner_id"])


def _vector_row(model, owner_field, owner_id, embedding, space=SPACE):
    return model(
        **{owner_field: owner_id},
        space=space.key,
        dim=space.dim,
        embedding=embedding,
        model=space.model,
    )


async def _corpus(client):
    """两个课题各一个库；主库里有好/坏/各种该被过滤的论文。返回 (headers, ids, 主库 id 列表)。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    main_pid, owner_id = await _project(client, headers, "vec-main")
    other_pid, _ = await _project(client, headers, "vec-other")
    ids = {}
    async with get_sessionmaker()() as session:
        specs = [
            ("best", main_pid, "compiled", BEST),
            ("good", main_pid, "included", GOOD),
            ("far", main_pid, "scored", FAR),
            ("trashed", main_pid, "excluded", BEST),  # 回收站
            ("candidate", main_pid, "candidate", BEST),  # 未筛选候选
            ("elsewhere", other_pid, "compiled", BEST),  # 别的库
            ("other-space", main_pid, "compiled", None),  # 只有别的空间的向量
            ("bad-dim", main_pid, "compiled", [1.0, 0.0]),  # 维度坏了的脏行
        ]
        for name, pid, status, vec in specs:
            paper = await add_paper(session, project_id=pid, title=f"Paper {name}", status=status)
            ids[name] = paper.id
            if vec is not None:
                session.add(_vector_row(PaperVector, "paper_id", paper.id, vec))
        session.add(
            _vector_row(PaperVector, "paper_id", ids["other-space"], BEST, space=OTHER_SPACE)
        )
        await session.commit()
        library_ids = await get_source_library_ids(session, main_pid)
    return headers, main_pid, owner_id, ids, library_ids


# ---- 各检索入口 ----


async def test_paper_search_ranks_within_library_and_filters(client):
    _headers, main_pid, _owner, ids, library_ids = await _corpus(client)
    async with get_sessionmaker()() as session:
        rows = await papers_service.semantic_search_papers(
            session, project_id=main_pid, query_vector=QUERY, space=SPACE, limit=10
        )
        assert [view.id for view, _ in rows] == [ids["best"], ids["good"], ids["far"]]
        assert rows[0][1] == pytest.approx(1.0)
        assert rows[1][1] == pytest.approx(_cos(QUERY, GOOD))

        limited = await papers_service.semantic_search_papers(
            session, library_ids=library_ids, query_vector=QUERY, space=SPACE, limit=2
        )
        assert [view.id for view, _ in limited] == [ids["best"], ids["good"]]


async def test_paper_search_counts_a_paper_in_two_libraries_once(client):
    _headers, main_pid, _owner, ids, library_ids = await _corpus(client)
    async with get_sessionmaker()() as session:
        other_lib = (
            await session.execute(
                LibraryPaper.__table__.select().where(LibraryPaper.paper_id == ids["elsewhere"])
            )
        ).first()
        session.add(LibraryPaper(library_id=other_lib.library_id, paper_id=ids["best"]))
        await session.commit()
        rows = await papers_service.semantic_search_papers(
            session,
            library_ids=[*library_ids, other_lib.library_id],
            query_vector=QUERY,
            space=SPACE,
            limit=10,
        )
    found = [view.id for view, _ in rows]
    assert found.count(ids["best"]) == 1
    assert set(found) == {ids["best"], ids["good"], ids["far"], ids["elsewhere"]}


async def test_project_search_api_is_semantic_on_sqlite(client, monkeypatch):
    headers, main_pid, _owner, ids, _library_ids = await _corpus(client)

    async def fake_embed_query(session, text, **_kwargs):
        return QUERY, SPACE

    monkeypatch.setattr("app.api.wiki.embed_query", fake_embed_query)
    resp = await client.get(
        f"/api/projects/{main_pid}/search",
        params={"q": "anything", "mode": "semantic"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode_used"] == "semantic"
    assert {row["id"] for row in body["papers"]} == {
        str(ids["best"]),
        str(ids["good"]),
        str(ids["far"]),
    }


async def _chunks(session, ids, spec):
    """spec: [(paper 名, 向量, 空间)] → 每篇一个分段，返回 {paper 名: chunk_id}。"""
    out = {}
    for name, vec, space in spec:
        chunk = PaperChunk(paper_id=ids[name], seq=0, text=f"chunk of {name}")
        session.add(chunk)
        await session.flush()
        session.add(_vector_row(PaperChunkVector, "chunk_id", chunk.id, vec, space=space))
        out[name] = chunk.id
    await session.commit()
    return out


async def test_chunk_search_by_library_filters_like_sql(client):
    _headers, _pid, _owner, ids, library_ids = await _corpus(client)
    async with get_sessionmaker()() as session:
        chunk_ids = await _chunks(
            session,
            ids,
            [
                ("best", BEST, SPACE),
                ("good", GOOD, SPACE),
                ("far", FAR, SPACE),
                ("trashed", BEST, SPACE),
                ("candidate", BEST, SPACE),
                ("elsewhere", BEST, SPACE),
                ("other-space", BEST, OTHER_SPACE),
                ("bad-dim", [1.0], SPACE),
            ],
        )
        rows = await chunks_service.semantic_search_chunks(
            session, library_ids=library_ids, query_vector=QUERY, space=SPACE, limit=10
        )
        assert [c.id for c, _ in rows] == [chunk_ids["best"], chunk_ids["good"], chunk_ids["far"]]
        assert rows[1][1] == pytest.approx(_cos(QUERY, GOOD))

        # 库 + paper_ids：两者都要满足
        rows = await chunks_service.semantic_search_chunks(
            session,
            library_ids=library_ids,
            query_vector=QUERY,
            space=SPACE,
            limit=10,
            paper_ids=[ids["far"], ids["elsewhere"]],
        )
        assert [c.id for c, _ in rows] == [chunk_ids["far"]]


async def test_chunk_search_by_paper_ids_ignores_membership(client):
    """纯 paper_ids 分支不 join 库（与 SQL 一致）：回收站状态不影响，但空间仍然要对。"""
    _headers, _pid, _owner, ids, _library_ids = await _corpus(client)
    async with get_sessionmaker()() as session:
        chunk_ids = await _chunks(
            session,
            ids,
            [
                ("far", FAR, SPACE),
                ("trashed", BEST, SPACE),
                ("other-space", BEST, OTHER_SPACE),
                ("good", GOOD, SPACE),
            ],
        )
        rows = await chunks_service.semantic_search_chunks(
            session,
            library_ids=None,
            query_vector=QUERY,
            space=SPACE,
            limit=2,
            paper_ids=[ids["far"], ids["trashed"], ids["other-space"], ids["good"]],
        )
    assert [c.id for c, _ in rows] == [chunk_ids["trashed"], chunk_ids["good"]]


async def test_saved_entries_search_scopes_to_saved_untrashed(client):
    _headers, _pid, owner_id, ids, _library_ids = await _corpus(client)
    async with get_sessionmaker()() as session:

        def entry(name, *, user_id=owner_id, saved=True, trashed=False):
            return UserLibraryEntry(
                user_id=user_id,
                dedup_key=f"k-{name}-{user_id}",
                title=f"Entry {name}",
                saved=saved,
                trashed_at=dt.datetime.now(dt.UTC) if trashed else None,
                last_paper_id=ids[name],
            )

        session.add_all(
            [
                entry("far"),
                entry("good"),
                entry("other-space"),
                entry("bad-dim"),
                entry("best", saved=False),  # 只是浏览过
                entry("trashed", trashed=True),  # 收藏进了回收站
            ]
        )
        await session.commit()
        rows = await user_library_service.semantic_saved_entries(
            session, user_id=owner_id, query_vector=QUERY, space=SPACE, limit=10
        )
    assert [e.last_paper_id for e, _ in rows] == [ids["good"], ids["far"]]
    assert rows[0][1] == pytest.approx(_cos(QUERY, GOOD))


async def test_daily_semantic_search_applies_feed_filters(client):
    await register_and_login(client)
    today = dt.date(2026, 10, 1)
    async with get_sessionmaker()() as session:
        entries = {}
        specs = [
            ("best", BEST, today, "cs.AI", "new"),
            ("good", GOOD, today, "cs.AI", "cross"),
            ("far", FAR, today, "cs.CL", "new"),
            ("old", BEST, today - dt.timedelta(days=3), "cs.AI", "new"),
            ("unsubscribed", BEST, today, "math.CO", "new"),
            ("other-space", None, today, "cs.AI", "new"),
        ]
        for name, vec, day, cat, announce in specs:
            paper = new_paper(title=f"Daily {name}")
            session.add(paper)
            await session.flush()
            if vec is not None:
                session.add(_vector_row(PaperVector, "paper_id", paper.id, vec))
            else:
                session.add(_vector_row(PaperVector, "paper_id", paper.id, BEST, space=OTHER_SPACE))
            entry = DailyFeedEntry(
                paper_id=paper.id,
                feed_date=day,
                primary_category=cat,
                categories=[cat],
                announce_type=announce,
            )
            session.add(entry)
            await session.flush()
            entries[name] = entry.id
        await session.commit()

        async def run(**kw):
            rows = await daily_service.semantic_search_daily(
                session, query_vector=QUERY, space=SPACE, limit=kw.pop("limit", 10), **kw
            )
            return [entry.id for entry, _paper, _score in rows]

        terms = ["cs.AI", "cs.CL"]
        assert await run(terms=terms, date=today) == [
            entries["best"],
            entries["good"],
            entries["far"],
        ]
        assert await run(terms=terms) == [
            entries["best"],
            entries["old"],
            entries["good"],
            entries["far"],
        ]
        assert await run(terms=terms, date=today, limit=1) == [entries["best"]]
        assert await run(terms=terms, date=today, category="cs.CL") == [entries["far"]]
        assert await run(terms=terms, date=today, announce="cross") == [entries["good"]]
        assert await run(terms=[]) == []  # 没订阅 = 什么都不给看


async def test_fulltext_vector_search_enforces_scope_and_grant(app, client):
    from sqlalchemy import select

    from app.services.paper_content import create_content_version, parse_content_version
    from tests.test_paper_content import _setup

    _user_row, library, _paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)
        await parse_content_version(session, version=version, mineru_parser=None)
        extra = [
            PaperContentChunk(content_version_id=version.id, seq=100 + i, text=f"extra {i}")
            for i in range(3)
        ]
        session.add_all(extra)
        await session.flush()
        for chunk, vec, space in [
            (extra[0], FAR, SPACE),
            (extra[1], BEST, SPACE),
            (extra[2], BEST, OTHER_SPACE),
        ]:
            session.add(
                PaperContentChunkVector(
                    chunk_id=chunk.id,
                    space=space.key,
                    dim=space.dim,
                    embedding=vec,
                    model=space.model,
                )
            )
        await session.flush()

        hits = await semantic_search_current_fulltext(
            session, library_ids=[library.id], query_vector=QUERY, space_key=SPACE.key, limit=5
        )
        assert [hit.chunk.id for hit in hits] == [extra[1].id, extra[0].id]
        assert all(hit.paper_id == asset.paper_id for hit in hits)
        assert hits[1].score == pytest.approx(_cos(QUERY, FAR))

        # 别的库看不到
        assert (
            await semantic_search_current_fulltext(
                session,
                library_ids=[uuid.uuid4()],
                query_vector=QUERY,
                space_key=SPACE.key,
                limit=5,
            )
            == []
        )
        # 资产授权撤掉后同样看不到
        grant = await session.scalar(
            select(AssetGrant).where(
                AssetGrant.asset_id == asset.id, AssetGrant.library_id == library.id
            )
        )
        grant.can_read = False
        await session.flush()
        assert (
            await semantic_search_current_fulltext(
                session,
                library_ids=[library.id],
                query_vector=QUERY,
                space_key=SPACE.key,
                limit=5,
            )
            == []
        )


# ---- 读取方式（#850）：不跨打分持有游标、打分不占事件循环、跨库段落检索有上限 ----

_ScanBase = declarative_base()


class _ScanRow(_ScanBase):
    """独立的小表：只测 vector_search 的读/算方式，不碰业务表。"""

    __tablename__ = "vs_scan_rows"
    id = Column(Integer, primary_key=True)
    space = Column(String, nullable=False)
    embedding = Column(JSON, nullable=False)


async def _scan_engine(tmp_path, n, dim, *, seed=0, timeout=5.0):
    """建一个文件型 SQLite（与线上同一种锁语义），塞 n 条 dim 维向量。"""
    import numpy as np

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/scan.db", connect_args={"timeout": timeout}
    )
    async with engine.begin() as conn:
        await conn.run_sync(_ScanBase.metadata.create_all)
        await conn.execute(text("CREATE TABLE IF NOT EXISTS vs_writes (x INTEGER)"))
    rng = np.random.default_rng(seed)
    matrix = rng.standard_normal((n, dim))
    # 造几组完全相同的向量：同分时的先后也要与「一次扫完」一致
    matrix[5] = matrix[3]
    matrix[n - 1] = matrix[3]
    async with AsyncSession(engine) as session:
        for start in range(0, n, 5000):
            session.add_all(
                [
                    _ScanRow(id=i, space="s", embedding=[float(x) for x in matrix[i]])
                    for i in range(start, min(n, start + 5000))
                ]
            )
            await session.commit()
    query = [float(x) for x in rng.standard_normal(dim)]
    return engine, matrix, query


def _scan_stmt():
    return select(_ScanRow.id, vector_search.raw_embedding(_ScanRow.embedding)).where(
        _ScanRow.space == "s"
    )


def _slow_scoring(monkeypatch, seconds):
    """让每批打分慢下来，把扫描拉长到能观察并发的程度（新旧实现都走 TopK.add）。"""
    import time

    original = vector_search.TopK.add

    def slow_add(self, rows):
        time.sleep(seconds)
        return original(self, rows)

    monkeypatch.setattr(vector_search.TopK, "add", slow_add)


async def test_chunked_search_matches_one_shot_scoring(tmp_path):
    """分批回查 + 线程打分，与把全部行一次性交给 top_k_cosine 的结果逐项一致（含同分）。"""
    engine, matrix, query = await _scan_engine(tmp_path, 257, 8)
    try:
        reference = vector_search.top_k_cosine(
            query, [(i, list(matrix[i])) for i in range(len(matrix))], 40
        )
        async with AsyncSession(engine) as session:
            for batch_size in (1, 7, 100, 1000):
                stats = vector_search.SearchStats()
                hits = await vector_search.search_statement(
                    session, _scan_stmt(), query, 40, batch_size=batch_size, stats=stats
                )
                assert [k for k, _ in hits] == [k for k, _ in reference], batch_size
                for (_, a), (_, b) in zip(hits, reference, strict=True):
                    assert a == pytest.approx(b)
                assert stats.candidates == stats.scanned == 257
    finally:
        await engine.dispose()


async def test_concurrent_write_succeeds_during_a_long_scan(tmp_path, monkeypatch):
    """扫描期间另一个连接提交写入：写入方只等一批的读取，不等整次扫描。

    旧实现开着流式游标打分，共享锁一直占到扫完，写入等满 busy timeout 后报
    "database is locked"。这里把写入方的 busy timeout 设得远小于扫描时长。
    """
    import asyncio
    import time

    engine, _matrix, query = await _scan_engine(tmp_path, 20000, 16, timeout=1.0)
    _slow_scoring(monkeypatch, 0.15)  # 20000 / 1000 = 20 批 → 扫描约 3 秒
    try:
        scan_done = asyncio.Event()

        async def scan():
            async with AsyncSession(engine) as session:
                hits = await vector_search.search_statement(session, _scan_stmt(), query, 10)
            scan_done.set()
            return hits

        async def write():
            await asyncio.sleep(0.5)
            async with AsyncSession(engine) as session:
                started = time.perf_counter()
                await session.execute(text("INSERT INTO vs_writes VALUES (1)"))
                await session.commit()
                return time.perf_counter() - started, scan_done.is_set()

        hits, (waited, scan_finished_first) = await asyncio.gather(scan(), write())
        assert len(hits) == 10
        assert not scan_finished_first, "写入应在扫描进行中完成"
        assert waited < 1.0
    finally:
        await engine.dispose()


async def test_scoring_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """打分在线程里跑：扫描期间另一个协程照常每 10ms 醒一次。"""
    import asyncio
    import time

    engine, _matrix, query = await _scan_engine(tmp_path, 3000, 16)
    _slow_scoring(monkeypatch, 0.2)  # 3 批，每批在线程里「算」0.2 秒
    gaps: list[float] = []
    try:

        async def ticker():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        async with AsyncSession(engine) as session:
            hits = await vector_search.search_statement(session, _scan_stmt(), query, 5)
        tick.cancel()
        assert len(hits) == 5
        assert len(gaps) > 20
        assert max(gaps) < 0.15, f"事件循环被卡住 {max(gaps):.3f}s"
    finally:
        await engine.dispose()


async def _narrowing_corpus(client):
    """主库里 best/good/far 三篇各 4 个分段；far 的论文级向量最不像，但它有一段最像。"""
    _headers, _pid, _owner, ids, library_ids = await _corpus(client)
    chunk_owner = {}
    async with get_sessionmaker()() as session:
        for name, vecs in {
            "best": [[2.0, 0.5, 0.0], GOOD, FAR, FAR],
            "good": [[2.0, 0.8, 0.0], FAR, FAR, FAR],
            "far": [BEST, FAR, FAR, FAR],  # 段落级最像，但论文级排第三
        }.items():
            for seq, vec in enumerate(vecs):
                chunk = PaperChunk(paper_id=ids[name], seq=seq, text=f"{name} chunk {seq}")
                session.add(chunk)
                await session.flush()
                session.add(_vector_row(PaperChunkVector, "chunk_id", chunk.id, vec))
                chunk_owner[chunk.id] = name
        await session.commit()
    return ids, library_ids, chunk_owner


def _count_scored_rows(monkeypatch):
    """数一数实际读出向量、交给打分的行（不靠 SearchStats，直接在打分入口计数）。"""
    seen: list[int] = []
    original = vector_search._add_in_key_order

    def counting(top, keys, rows):
        seen.append(len(rows))
        return original(top, keys, rows)

    monkeypatch.setattr(vector_search, "_add_in_key_order", counting)
    return seen


async def test_chunk_search_narrows_to_top_papers_over_budget(client, monkeypatch):
    from app.core.config import get_settings

    ids, library_ids, chunk_owner = await _narrowing_corpus(client)

    # 预算之内：与从前一样逐段全扫，far 那段最像排第一
    async with get_sessionmaker()() as session:
        stats = vector_search.SearchStats()
        rows = await chunks_service.semantic_search_chunks(
            session, library_ids=library_ids, query_vector=QUERY, space=SPACE, limit=3, stats=stats
        )
    assert chunk_owner[rows[0][0].id] == "far"
    assert stats.narrowed_to_papers is None
    assert stats.candidates == stats.scanned == 12

    # 超出预算：先按论文级向量挑 2 篇（best、good），只在它们的 8 段里排
    monkeypatch.setattr(get_settings(), "vector_search_row_budget", 5)
    monkeypatch.setattr(get_settings(), "vector_search_narrow_papers", 2)
    scored = _count_scored_rows(monkeypatch)
    async with get_sessionmaker()() as session:
        stats = vector_search.SearchStats()
        rows = await chunks_service.semantic_search_chunks(
            session, library_ids=library_ids, query_vector=QUERY, space=SPACE, limit=3, stats=stats
        )
    assert [chunk_owner[c.id] for c, _ in rows] == ["best", "good", "best"]
    assert rows[0][1] == pytest.approx(_cos(QUERY, [2.0, 0.5, 0.0]))
    assert stats.candidates == 12
    assert stats.narrowed_to_papers == 2
    assert stats.scanned == 8
    # 打分入口实际收到的行：论文级 4 条（best/good/far/bad-dim 的论文向量）+ 收窄后的
    # 8 段——全部 12 段的向量从来没被读出来过
    assert scored == [4, 8]

    # 显式给了 paper_ids 的调用（伴读/个人库）不收窄
    async with get_sessionmaker()() as session:
        rows = await chunks_service.semantic_search_chunks(
            session,
            library_ids=library_ids,
            query_vector=QUERY,
            space=SPACE,
            limit=1,
            paper_ids=[ids["far"]],
        )
    assert chunk_owner[rows[0][0].id] == "far"


async def test_search_chunks_tool_reports_narrowing(client, monkeypatch):
    """全局助手的段落检索：跨全部库时收窄，并在结果里如实说明。"""
    import app.tools as tools
    from app.core.config import get_settings
    from app.core.llm.router import LLMRouter
    from app.tools import ToolContext
    from app.tools.scope import all_library_ids

    ids, library_ids, _chunk_owner = await _narrowing_corpus(client)
    async with get_sessionmaker()() as session:
        everything = tuple(await all_library_ids(session))
    assert set(library_ids) <= set(everything)

    async def fake_embed_query(session, text, **_kwargs):
        return QUERY, SPACE

    monkeypatch.setattr("app.tools.knowledge.embed_query", fake_embed_query)
    monkeypatch.setattr(get_settings(), "vector_search_row_budget", 5)
    monkeypatch.setattr(get_settings(), "vector_search_narrow_papers", 2)
    ctx = ToolContext(project_id=None, llm=LLMRouter(), library_ids=everything)
    res = await tools.run_tool(ctx, "search_chunks", {"query": "anything", "k": 3})
    assert res["mode"] == "semantic"
    assert res["narrowed_to_papers"] == 2
    assert res["note"]
    assert {c["paper_id"] for c in res["chunks"]} <= {str(ids["best"]), str(ids["good"])}
