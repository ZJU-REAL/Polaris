"""索引覆盖：无全文的论文也有可检索分段 + 索引状态查询 + 手动重建。

过去分段只从 PDF 全文生成，没有全文的论文（每日推送尤其严重——抓取时根本不下 PDF）
对文献对话完全不存在。本文件覆盖：
- 无全文入库 → 一个「标题 + 摘要」兜底块，且真能被检索到；
- 拿到全文后兜底块被全文块整体替换；
- 分块向量受用户开关控制且默认开；
- 状态端点返回两种向量的有无 / 构建时间 / 模型名；
- 手动重建覆盖旧向量。
"""

import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperChunk
from app.services import paper_enrich
from app.services.chunks import ensure_paper_chunks, keyword_search_chunks
from tests.conftest import add_paper, make_project_with_library, register_and_login
from tests.vector_helpers import (
    get_chunk_vector,
    get_paper_vector,
    set_chunk_vector,
    set_paper_vector,
)

pytestmark = pytest.mark.asyncio


async def _noop_emit(stage: str, status: str, detail: str | None = None) -> None:
    return None


def _write_fulltext(text: str = "规划方法的实现细节与实验。") -> str:
    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-idx-status-"))
    txt = txt_dir / "p.txt"
    txt.write_text(text * 200, encoding="utf-8")
    return str(txt)


async def _user(client, email: str):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()
    return uuid.UUID(me["id"]), headers


async def _chunks(session, paper_id) -> list[PaperChunk]:
    return list(
        (
            await session.execute(
                select(PaperChunk)
                .where(PaperChunk.paper_id == paper_id)
                .order_by(PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )


# ---- 1. 无全文 → 摘要兜底块，且可被检索到 ----


async def test_paper_without_fulltext_gets_single_abstract_chunk(client):
    """没有全文的论文入库后有且仅有一个摘要块，来源标成 abstract。"""
    _, headers = await _user(client, "abs@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="abs")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Tree Search Planning for Agents",
            abstract="We propose a tree search planner for language agents.",
            doi="10.1/abs",
        )
        await session.commit()
        paper_id = paper.id

        assert await ensure_paper_chunks(session, paper) == 1
        await session.commit()

        chunks = await _chunks(session, paper_id)
    assert len(chunks) == 1
    assert chunks[0].source == "abstract"
    # 标题与摘要都进了块（「找某人在讲什么」两头都得能命中）
    assert "Tree Search Planning for Agents" in chunks[0].text
    assert "tree search planner" in chunks[0].text


async def test_abstract_chunk_is_retrievable(client):
    """兜底块进得了检索：关键词检索能命中它（sqlite 下走降级关键词分支）。"""
    _, headers = await _user(client, "retrieve@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="retrieve")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Reflexion Self Improvement",
            abstract="An agent reflects on failures to improve future attempts.",
            doi="10.1/retrieve",
        )
        await session.commit()
        paper_id = paper.id
        await ensure_paper_chunks(session, paper)
        await session.commit()

        hits = await keyword_search_chunks(
            session, library_ids=None, q="reflexion agent", limit=10, paper_ids=[paper_id]
        )
    assert hits, "摘要兜底块必须能被检索到——否则这篇论文对文献对话依然不存在"
    assert all(c.paper_id == paper_id for c, _ in hits)


async def test_fulltext_replaces_abstract_chunk(client):
    """先无全文建了摘要块，之后拿到全文 → 摘要块被全文块整体替换。"""
    _, headers = await _user(client, "replace@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="replace")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Later Gets Fulltext",
            abstract="Short abstract.",
            doi="10.1/replace",
        )
        await session.commit()
        paper_id = paper.id
        await ensure_paper_chunks(session, paper)
        await session.commit()
        assert [c.source for c in await _chunks(session, paper_id)] == ["abstract"]

        # 拿到 PDF 全文后再跑一次
        paper.full_text_path = _write_fulltext()
        await session.commit()
        assert await ensure_paper_chunks(session, paper) > 1
        await session.commit()

        chunks = await _chunks(session, paper_id)
    assert len(chunks) > 1
    assert {c.source for c in chunks} == {"fulltext"}  # 摘要块没留下
    assert all("Short abstract." not in c.text for c in chunks)


async def test_existing_fulltext_chunks_are_not_resliced(client):
    """已有全文块的论文再 ensure 不重切（重切会丢已补的块向量）。"""
    _, headers = await _user(client, "keepft@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="keepft")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Keep Fulltext",
            doi="10.1/keepft",
            full_text_path=_write_fulltext(),
        )
        await session.commit()
        paper_id = paper.id
        chunk = PaperChunk(paper_id=paper_id, seq=0, text="手工全文块", source="fulltext")
        session.add(chunk)
        await session.commit()
        await set_chunk_vector(session, chunk.id, [0.5] * 1024)

        assert await ensure_paper_chunks(session, paper) == 0
        chunks = await _chunks(session, paper_id)
    assert len(chunks) == 1 and chunks[0].text == "手工全文块"


# ---- 1b. 摘要块的向量 = 论文级向量的拷贝（零 token） ----


async def test_abstract_chunk_copies_paper_vector(client):
    """摘要块不另调嵌入接口：向量直接拷论文级向量（用哨兵向量证明是拷贝而非重算）。"""
    _, headers = await _user(client, "copyvec@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="copyvec")
    sentinel = [0.125] * 1024
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Copy My Vector",
            abstract="The chunk vector must be a copy.",
            doi="10.1/copyvec",
        )
        await session.commit()
        paper_id = paper.id
        await set_paper_vector(session, paper_id, sentinel)

        await ensure_paper_chunks(session, paper)
        await session.commit()

        chunks = await _chunks(session, paper_id)
        copied = await get_chunk_vector(session, chunks[0].id)
    assert len(chunks) == 1 and chunks[0].source == "abstract"
    assert copied == pytest.approx(sentinel), "摘要块向量应是论文级向量的拷贝"


async def test_abstract_chunk_vector_filled_in_later(client):
    """论文级向量当时还没建好 → 摘要块先留空，之后一次 sync 补上，不报错。"""
    from app.services.chunks import sync_abstract_chunk_vectors

    _, headers = await _user(client, "latevec@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="latevec")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Vector Comes Later",
            abstract="No paper vector yet.",
            doi="10.1/latevec",
        )
        await session.commit()
        paper_id = paper.id

        await ensure_paper_chunks(session, paper)  # 此刻还没有论文级向量
        await session.commit()
        chunk_id = (await _chunks(session, paper_id))[0].id
        assert await get_chunk_vector(session, chunk_id) is None

        # 论文级向量后来建好了
        sentinel = [0.375] * 1024
        await set_paper_vector(session, paper_id, sentinel)
        assert await sync_abstract_chunk_vectors(session, paper_ids=[paper_id]) == 1
        await session.commit()

        filled = await get_chunk_vector(session, chunk_id)
    assert filled == pytest.approx(sentinel)


async def test_library_rebuild_copies_instead_of_embedding_abstract(client):
    """整库重建时摘要块也不花嵌入调用：向量拷论文级向量，用 abstract_vectors_copied 计数。"""
    _, headers = await _user(client, "librebuild@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="librebuild")
    sentinel = [0.625] * 1024
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Abstract Only In Library",
            abstract="Copied vector please.",
            doi="10.1/librebuild",
        )
        await session.commit()
        paper_id = paper.id
        await set_paper_vector(session, paper_id, sentinel)  # 论文级向量已就位

    resp = await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks_created"] == 1
    assert body["embedded"] == 0, "摘要块不该占用嵌入调用"
    # 论文级向量此刻已就位 → 建块时就地拷好了，无需 sync 事后补（补的计数因此是 0）
    assert body["abstract_vectors_copied"] == 0

    async with get_sessionmaker()() as session:
        chunks = await _chunks(session, paper_id)
        copied = await get_chunk_vector(session, chunks[0].id)
    assert chunks[0].source == "abstract"
    assert copied == pytest.approx(sentinel), "向量应是论文级向量的拷贝"


# ---- 1c. 论文级向量的管理员总闸（默认开） ----


async def _run_enrich(paper_id, *, user_id):
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=user_id, project_id=None, emit=_noop_emit
        )


async def _seed_for_enrich(client, headers, *, name: str, email_key: str) -> uuid.UUID:
    project_id, _ = await make_project_with_library(client, headers, name=name)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title=f"Paper {name}",
            doi=f"10.1/{email_key}",
            full_text_path=_write_fulltext(),
        )
        await session.commit()
        return paper.id


async def test_vectors_are_always_built(client, fake_redis):
    """论文级向量与块向量都没有开关，入库就一定建。

    这两者曾经各有一个开关（管理员的 paper_embedding_enabled、用户的
    chat_fulltext_index），都已废除：库同步靠向量相似度粗排每日论文池，向量成了检索的
    承重结构——关掉任何一个都会让同步瞎掉，而界面上只会显示「0 篇」，无从察觉。
    """
    user_id, headers = await _user(client, "always-on@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="always-on", email_key="alwayson")

    await _run_enrich(paper_id, user_id=user_id)

    async with get_sessionmaker()() as session:
        paper_vector = await get_paper_vector(session, paper_id)
        chunks = await _chunks(session, paper_id)
        chunk_vectors = [await get_chunk_vector(session, c.id) for c in chunks]
    assert paper_vector is not None, "论文级向量必须建"
    assert chunks
    assert all(v is not None for v in chunk_vectors), "块向量必须建"


async def test_no_endpoint_can_turn_paper_embedding_off(client):
    """管理员总闸连同端点一起下线了——留着入口就等于留着把检索关瞎的办法。"""
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}  # 首个用户=admin
    resp = await client.get("/api/admin/settings/paper-embedding", headers=headers)
    assert resp.status_code == 404
    assert (
        await client.put(
            "/api/admin/settings/paper-embedding", json={"enabled": False}, headers=headers
        )
    ).status_code == 404


# ---- 3. 状态端点 ----


async def test_index_status_reports_time_and_model(client, fake_redis):
    """状态端点给出两种向量的有无、构建时间与模型名。"""
    user_id, headers = await _user(client, "status@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="status", email_key="status")

    # 建之前：两种向量都没有，时间/模型为空
    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_vector"] == {
        "built": False,
        "built_at": None,
        "model": None,
        "stale": False,
    }
    assert body["chunk_vector"]["built"] is False

    await _run_enrich(paper_id, user_id=user_id)

    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    body = resp.json()
    assert body["paper_vector"]["built"] is True
    assert body["paper_vector"]["built_at"] and body["paper_vector"]["model"]
    assert body["chunk_vector"]["built"] is True
    assert body["chunk_vector"]["built_at"] and body["chunk_vector"]["model"]
    assert body["chunk_count"] > 0
    assert body["embedded_chunk_count"] == body["chunk_count"]
    assert body["has_fulltext"] is True
    assert body["chunk_source"] == "fulltext"


async def test_index_status_for_paper_without_fulltext(client):
    """没有全文的论文：状态如实报 abstract 来源、has_fulltext=False。"""
    _, headers = await _user(client, "status-abs@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="status-abs")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="No Fulltext Here",
            abstract="Just an abstract.",
            doi="10.1/statusabs",
        )
        await session.commit()
        paper_id = paper.id
        await ensure_paper_chunks(session, paper)
        await session.commit()

    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    body = resp.json()
    assert body["has_fulltext"] is False
    assert body["chunk_source"] == "abstract"
    assert body["chunk_count"] == 1


# ---- 4. 手动重建 ----


async def test_manual_rebuild_overwrites_existing_vectors(client, fake_redis):
    """手动重建覆盖旧向量：预置的哨兵向量必须被换掉。"""
    user_id, headers = await _user(client, "rebuild@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="rebuild", email_key="rebuild")
    await _run_enrich(paper_id, user_id=user_id)

    sentinel = [0.25] * 1024
    async with get_sessionmaker()() as session:
        await set_paper_vector(session, paper_id, sentinel)
        for chunk in await _chunks(session, paper_id):
            await set_chunk_vector(session, chunk.id, sentinel)

    resp = await client.post(f"/api/papers/{paper_id}/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_vector"]["built"] is True
    assert body["chunk_vector"]["built"] is True
    assert body["embedded_chunk_count"] == body["chunk_count"] > 0

    async with get_sessionmaker()() as session:
        paper_vector = await get_paper_vector(session, paper_id)
        chunks = await _chunks(session, paper_id)
        chunk_vectors = [await get_chunk_vector(session, c.id) for c in chunks]
    assert paper_vector != pytest.approx(sentinel), "论文级向量应被重建覆盖"
    assert all(v is not None for v in chunk_vectors)
    assert all(v != pytest.approx(sentinel) for v in chunk_vectors), "块向量应被重建覆盖"


async def test_manual_rebuild_only_paper_vector(client, fake_redis):
    """只重建论文级向量时不动分块向量。"""
    user_id, headers = await _user(client, "rebuild-one@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="rebuild-one", email_key="rbone")
    await _run_enrich(paper_id, user_id=user_id)

    sentinel = [0.25] * 1024
    async with get_sessionmaker()() as session:
        for chunk in await _chunks(session, paper_id):
            await set_chunk_vector(session, chunk.id, sentinel)

    resp = await client.post(
        f"/api/papers/{paper_id}/index/rebuild",
        json={"paper_vector": True, "chunks": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async with get_sessionmaker()() as session:
        chunks = await _chunks(session, paper_id)
        chunk_vectors = [await get_chunk_vector(session, c.id) for c in chunks]
    assert all(v == pytest.approx(sentinel) for v in chunk_vectors), "没请求重建就别动"


async def test_manual_rebuild_builds_index_from_scratch(client, fake_redis):
    """从没建过索引的论文（无全文）也能一键建出来：摘要块 + 两种向量。"""
    _, headers = await _user(client, "rebuild-scratch@example.com")
    project_id, _ = await make_project_with_library(client, headers, name="scratch")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Never Indexed",
            abstract="This paper was never indexed.",
            doi="10.1/scratch",
        )
        await session.commit()
        paper_id = paper.id

    resp = await client.post(f"/api/papers/{paper_id}/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_vector"]["built"] is True
    assert body["chunk_vector"]["built"] is True
    assert body["chunk_count"] == 1 and body["chunk_source"] == "abstract"


async def test_index_endpoints_require_visibility(client, fake_redis):
    """权限 = 能看到这篇论文（+ 重建还要有大模型使用权限）。

    孤儿论文（不在任何库、不在每日推送池、没人上架）谁都读不到，两个端点一律 404。
    注意在公共库里的论文本来就全实验室可读（P5c），那不是越权。
    """
    _, headers = await _user(client, "orphan-idx@example.com")
    async with get_sessionmaker()() as session:
        paper = Paper(title="Orphan Paper", abstract="Nobody can reach me.")
        session.add(paper)
        await session.commit()
        paper_id = paper.id

    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    assert resp.status_code == 404
    resp = await client.post(f"/api/papers/{paper_id}/index/rebuild", headers=headers)
    assert resp.status_code == 404


async def test_rebuild_blocked_for_users_without_llm_access(client, fake_redis):
    """大模型权限被锁的用户不能点重建（重建要花嵌入调用）。"""
    from app.models.user import User

    user_id, headers = await _user(client, "blocked-idx@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="blocked", email_key="blocked")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        user.llm_access = "blocked"
        await session.commit()

    resp = await client.post(f"/api/papers/{paper_id}/index/rebuild", headers=headers)
    assert resp.status_code == 403
    # 只读的状态查询不受影响
    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    assert resp.status_code == 200


async def test_rebuild_rejects_empty_target(client, fake_redis):
    _, headers = await _user(client, "empty-target@example.com")
    paper_id = await _seed_for_enrich(client, headers, name="empty", email_key="empty")
    resp = await client.post(
        f"/api/papers/{paper_id}/index/rebuild",
        json={"paper_vector": False, "chunks": False},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "NOTHING_TO_REBUILD"


async def test_index_status_readable_for_a_daily_feed_paper(client):
    """每日论文（只在内容池、不属于任何文献库）也要能看到索引状态。

    每日论文详情页会显示这两个红绿点，而那些论文没有任何库成员行——端点必须
    include_pool 才读得到，否则界面上是一片 404。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry
    from app.models.paper import new_paper

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    async with get_sessionmaker()() as session:
        paper = new_paper(source="arxiv", arxiv_id="2607.55001", title="Pool Only", abstract="x")
        session.add(paper)
        await session.flush()
        session.add(
            DailyFeedEntry(
                paper_id=paper.id, feed_date=dt.date(2026, 7, 28), primary_category="cs.AI"
            )
        )
        await session.commit()
        paper_id = paper.id

    resp = await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "paper_vector" in body and "chunk_vector" in body
    assert body["paper_vector"]["built"] is False  # 刚入池，还没建向量
