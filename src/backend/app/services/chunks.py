"""论文分段索引（文献知识底座，不 import fastapi）。

- 切分：确定性代码，按段落边界贪心打包到 ~CHUNK_TARGET_CHARS，超长段硬切带重叠；
- 兜底：没有全文的论文退化成一个「标题 + 摘要」块（source="abstract"），保证每篇
  论文对检索都存在——否则每日推送这类不下 PDF 的论文对文献对话完全不可见；
- 嵌入：wiki.link_concepts 步骤批量补齐（provider 不支持时留空）；
- 检索：postgres 走 pgvector 余弦，其他方言 / 无向量时降级关键词打分（两类块不区分）。
支撑文献库对话（services/library_chat.py）与后续 idea 生成等知识服务。
"""

import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import LLMRouter
from app.models.base import utcnow
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperChunk

CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 1600  # 超过则硬切
CHUNK_OVERLAP_CHARS = 150
MAX_CHUNKS_PER_PAPER = 120
EMBED_BATCH = 32

_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
_WORD_RE = re.compile(r"[\w一-鿿]+")


def split_text(full_text: str) -> list[str]:
    """确定性切分：段落边界优先，贪心打包；超长段硬切并带少量重叠。"""
    paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(full_text or "") if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= CHUNK_MAX_CHARS:
            pieces.append(para)
            continue
        start = 0
        while start < len(para):
            pieces.append(para[start : start + CHUNK_MAX_CHARS])
            start += CHUNK_MAX_CHARS - CHUNK_OVERLAP_CHARS

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for piece in pieces:
        if size + len(piece) > CHUNK_TARGET_CHARS and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(piece)
        size += len(piece)
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks[:MAX_CHUNKS_PER_PAPER]


def abstract_chunk_text(paper: Paper) -> str:
    """没有全文时的兜底块文本 = 论文级向量的文本口径（标题 + 作者 + 摘要，截 2000 字）。

    刻意与 ``paper_embedding_text`` 用同一份文本：兜底块的向量是**直接拷贝**论文级
    向量的（见 :func:`sync_abstract_chunk_vectors`），文本口径不一致的话，块里写的
    和向量表达的就不是一回事，检索命中后给模型的片段会对不上。
    """
    from app.services.paper_enrich import paper_embedding_text

    return paper_embedding_text(paper).strip()


async def replace_chunks(
    session: AsyncSession, paper: Paper, full_text: str, *, source: str = "fulltext"
) -> int:
    """重建一篇论文的分段（旧分段**全部**删除；embedding 留空待批量补齐）。调用方负责 commit。

    先删后建，所以拿到 PDF 后重建全文分段会把此前的摘要兜底块一并替换掉。
    入库前再清洗一遍控制字符：磁盘上历史抽取的 txt 可能仍带 0x00，postgres UTF8 会拒绝。
    """
    from app.services.literature.pdf_extract import sanitize_text

    await session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper.id))
    chunks = split_text(sanitize_text(full_text))
    for seq, chunk_text in enumerate(chunks):
        session.add(PaperChunk(paper_id=paper.id, seq=seq, text=chunk_text, source=source))
    return len(chunks)


async def index_paper_fulltext(session: AsyncSession, paper: Paper) -> int:
    """从 full_text_path 读全文并重建分段；无全文返回 0。调用方负责 commit。"""
    if not paper.full_text_path or not Path(paper.full_text_path).exists():
        return 0
    full_text = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
    return await replace_chunks(session, paper, full_text)


async def index_paper_abstract(session: AsyncSession, paper: Paper) -> int:
    """无全文兜底：用标题+作者+摘要建**单个**块（source="abstract"）。调用方负责 commit。

    向量**直接拷贝论文级向量**，不另调嵌入接口——两者文本口径完全相同，再算一遍纯属
    浪费 token，还会让同一篇论文的两个向量出现细微差别。论文级向量此刻还没建好
    （嵌入接口当时失败、或管理员关了总闸）就先留空，等下次
    :func:`sync_abstract_chunk_vectors` 补上，不报错。

    连标题都没有（脏数据）返回 0。已有的分段先删——调用方只在「没有全文块」时才走
    到这里，删掉的至多是上一版摘要块。
    """
    text = abstract_chunk_text(paper)
    if not text:
        return 0
    await session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper.id))
    chunk = PaperChunk(
        paper_id=paper.id, seq=0, text=text[:CHUNK_MAX_CHARS], source="abstract"
    )
    # 只在真有向量时才赋值：sqlite 分支 embedding 是 JSON 列，显式写 None 会存成
    # JSON 'null' 而不是 SQL NULL，之后 `embedding IS NULL` 就再也筛不到这一行了。
    if paper.embedding is not None:
        chunk.embedding = paper.embedding
        paper.chunk_embedding_at = paper.embedding_at
        paper.chunk_embedding_model = paper.embedding_model
    session.add(chunk)
    return 1


async def sync_abstract_chunk_vectors(
    session: AsyncSession, *, paper_ids: list[uuid.UUID]
) -> int:
    """把论文级向量拷进还没有向量的摘要兜底块，返回补上的块数。调用方负责 commit。

    零 token 开销，所以不受任何开关控制——摘要块的存在与否决定了这篇论文能不能被
    文献对话检索到，不该因为用户关了「全文索引」（那管的是花钱建全文块向量）就退回
    「整篇论文搜不到」。论文级向量必须先建好，故所有调用点都排在嵌入之后。
    """
    if not paper_ids:
        return 0
    rows = (
        (
            await session.execute(
                select(PaperChunk, Paper)
                .join(Paper, Paper.id == PaperChunk.paper_id)
                .where(
                    PaperChunk.paper_id.in_(list(set(paper_ids))),
                    PaperChunk.source == "abstract",
                    PaperChunk.embedding.is_(None),
                    Paper.embedding.is_not(None),
                )
            )
        )
        .all()
    )
    for chunk, paper in rows:
        chunk.embedding = paper.embedding
        paper.chunk_embedding_at = paper.embedding_at
        paper.chunk_embedding_model = paper.embedding_model
    return len(rows)


async def ensure_paper_chunks(session: AsyncSession, paper: Paper) -> int:
    """确保论文有可检索分段，返回**新建**的块数（已就绪返回 0）。调用方负责 commit。

    所有入库路径共用的统一入口：
    - 有全文且尚无全文块 → 按全文切（顺带替换掉此前的摘要兜底块）；
    - 有全文且已有全文块 → 不动（重切会丢已补的块向量）；
    - 无全文且完全没有块 → 建一个「标题 + 摘要」兜底块。

    注意判据是「有没有**全文**块」而不是「有没有块」：摘要兜底块一旦存在，按后者
    判断的话这篇论文就再也拿不到全文分段了。
    """
    if paper.full_text_path and Path(paper.full_text_path).exists():
        if await paper_has_chunks(session, paper.id, source="fulltext"):
            return 0
        return await index_paper_fulltext(session, paper)
    if await paper_has_chunks(session, paper.id):
        return 0
    return await index_paper_abstract(session, paper)


async def paper_has_chunks(
    session: AsyncSession, paper_id: uuid.UUID, *, source: str | None = None
) -> bool:
    """该论文是否已有分段行（给了 source 就只看该来源的）。

    用于「无块才切」——避免重切丢已补的块向量。
    """
    stmt = select(PaperChunk.id).where(PaperChunk.paper_id == paper_id)
    if source is not None:
        stmt = stmt.where(PaperChunk.source == source)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _embed_chunks(
    session: AsyncSession,
    pending: list[PaperChunk],
    *,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[int, str | None]:
    """分批嵌入给定的待补分段（调用方负责选出 pending），返回 (成功条数, 错误说明|None)。

    顺带把「分块向量建于何时、用的哪个模型」记到所属论文行上（前端索引状态展示用）。
    """
    embedded = 0
    model = await llm.model_name("embedding", user_id)
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        try:
            vectors = await llm.embed(
                [c.text[:2000] for c in batch],
                user_id=user_id,
                project_id=project_id,
                library_id=library_id,
                voyage_id=voyage_id,
            )
        except NotImplementedError:
            return embedded, "provider does not support embeddings"
        except Exception as e:  # noqa: BLE001 — 嵌入失败不影响主流程
            return embedded, f"{type(e).__name__}: {e}"
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.embedding = vector
            embedded += 1
        await _touch_chunk_embedding_meta(
            session, {c.paper_id for c in batch}, model=model
        )
        await session.commit()
    return embedded, None


async def _touch_chunk_embedding_meta(
    session: AsyncSession, paper_ids: set[uuid.UUID], *, model: str | None
) -> None:
    """记下这批论文的分块向量构建时间与模型名（调用方负责 commit）。"""
    if not paper_ids:
        return
    now = utcnow()
    papers = (
        (await session.execute(select(Paper).where(Paper.id.in_(list(paper_ids)))))
        .scalars()
        .all()
    )
    for paper in papers:
        paper.chunk_embedding_at = now
        paper.chunk_embedding_model = model


async def embed_pending_chunks(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    limit: int = 2000,
) -> tuple[int, str | None]:
    """批量补齐库内论文缺失的分段向量，返回 (成功条数, 错误说明|None)。

    project_id 仅用于 LLM 用量记账归属。
    """
    pending = list(
        (
            await session.execute(
                select(PaperChunk)
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(
                    LibraryPaper.library_id == library_id,
                    PaperChunk.embedding.is_(None),
                    # 摘要兜底块的向量靠拷贝论文级向量（零开销），不走嵌入接口
                    PaperChunk.source == "fulltext",
                )
                .order_by(PaperChunk.created_at, PaperChunk.seq)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return await _embed_chunks(
        session,
        pending,
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,  # 全文向量化随库 ingest 记方向库账（P6）
        voyage_id=voyage_id,
    )


async def embed_pending_chunks_for_papers(
    session: AsyncSession,
    *,
    paper_ids: list[uuid.UUID],
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    limit: int = 2000,
) -> tuple[int, str | None]:
    """按论文集合批量补齐缺失的分段向量（不 join 方向库），返回 (成功条数, 错误说明|None)。

    用于「可选全文索引」按 scope 论文集合建索引；记账归 user_id（不记方向库账）。
    """
    if not paper_ids:
        return 0, None
    pending = list(
        (
            await session.execute(
                select(PaperChunk)
                .where(
                    PaperChunk.paper_id.in_(paper_ids),
                    PaperChunk.embedding.is_(None),
                    # 摘要兜底块的向量靠拷贝论文级向量（零开销），不走嵌入接口
                    PaperChunk.source == "fulltext",
                )
                .order_by(PaperChunk.created_at, PaperChunk.seq)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return await _embed_chunks(
        session,
        pending,
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        voyage_id=voyage_id,
    )


async def rebuild_library_fulltext_index(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """重建某个库的分段索引（docs/api-lit.md §8）：给缺分段的论文补分段并嵌入。

    幂等：已有全文分段的论文跳过；没有全文的论文补一个「标题 + 摘要」兜底块，
    这样整库论文都能被文献对话检索到。新入库论文由 ingest 流水线自动处理，
    通常无需手动调用。课题作用域与库作用域两个端点共用本函数
    （project_id 仅用于 LLM 用量记账归属）。
    paper_chunks 表尚未迁移时 ``ProgrammingError`` 原样上抛，由路由层转 503。
    """
    papers = (
        (
            await session.execute(
                select(Paper)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(LibraryPaper.library_id == library_id)
            )
        )
        .scalars()
        .all()
    )
    indexed = 0
    chunk_count = 0
    for paper in papers:
        n = await ensure_paper_chunks(session, paper)
        if n:
            indexed += 1
            chunk_count += n
    await session.commit()
    embedded, embed_error = await embed_pending_chunks(
        session,
        library_id=library_id,
        llm=llm,
        user_id=user_id,
        project_id=project_id,
    )
    # 摘要兜底块不走嵌入接口，向量拷论文级向量（论文级向量也还没建的就先留空）
    copied = await sync_abstract_chunk_vectors(
        session, paper_ids=[p.id for p in papers]
    )
    await session.commit()
    total_chunks = int(
        (
            await session.execute(
                select(func.count())
                .select_from(PaperChunk)
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(LibraryPaper.library_id == library_id)
            )
        ).scalar_one()
    )
    return {
        "papers_indexed": indexed,
        "chunks_created": chunk_count,
        # embedded 只数「花了嵌入调用的全文块」；摘要块的向量是拷来的，单列 copied
        "embedded": embedded,
        "abstract_vectors_copied": copied,
        "embed_error": embed_error,
        "total_chunks": total_chunks,
    }


# ---- 检索 ----


def chunk_vector_search_supported(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


async def semantic_search_chunks(
    session: AsyncSession,
    *,
    library_ids: list[uuid.UUID] | None = None,
    query_vector: list[float],
    limit: int,
    paper_ids: list[uuid.UUID] | None = None,
) -> list[tuple[PaperChunk, float]]:
    """pgvector 余弦检索（仅 postgres；调用方需先判 chunk_vector_search_supported）。

    默认按 library_ids 限定关联库并集（chunk 命中多库时 DISTINCT 去重）。
    传 paper_ids 时把检索限制在这些论文内（伴读引用指定文献用）。
    library_ids 为空/None 且给了 paper_ids 时走「纯 paper_ids、不 join 库」分支
    （课题相关研究 / 个人库对话按论文集合直接检索）。
    """
    qv = json.dumps(query_vector)
    if not library_ids and paper_ids:
        # 纯 paper_ids 分支：不 join library_papers，只按论文集合过滤
        params: dict[str, object] = {
            "qv": qv,
            "pids": [str(p) for p in paper_ids],
            "k": limit,
        }
        rows = (
            await session.execute(
                sa_text(
                    "SELECT c.id, 1 - (c.embedding <=> CAST(:qv AS vector)) AS score "
                    "FROM paper_chunks c "
                    "WHERE c.embedding IS NOT NULL "
                    "AND c.paper_id = ANY(CAST(:pids AS uuid[])) "
                    "ORDER BY score DESC "
                    "LIMIT :k"
                ),
                params,
            )
        ).all()
    else:
        params = {
            "qv": qv,
            "libs": [str(lid) for lid in (library_ids or [])],
            "k": limit,
        }
        paper_filter = ""
        if paper_ids:
            paper_filter = "AND c.paper_id = ANY(CAST(:pids AS uuid[])) "
            params["pids"] = [str(p) for p in paper_ids]
        rows = (
            await session.execute(
                sa_text(
                    "SELECT DISTINCT c.id, 1 - (c.embedding <=> CAST(:qv AS vector)) AS score "
                    "FROM paper_chunks c "
                    "JOIN library_papers lp ON lp.paper_id = c.paper_id "
                    "AND lp.library_id = ANY(CAST(:libs AS uuid[])) "
                    "WHERE c.embedding IS NOT NULL "
                    f"{paper_filter}"
                    "ORDER BY score DESC "
                    "LIMIT :k"
                ),
                params,
            )
        ).all()
    if not rows:
        return []
    scores = {row.id: float(row.score) for row in rows}
    chunks = (
        (await session.execute(select(PaperChunk).where(PaperChunk.id.in_(list(scores)))))
        .scalars()
        .all()
    )
    by_id = {c.id: c for c in chunks}
    return [(by_id[row.id], scores[row.id]) for row in rows if row.id in by_id]


async def keyword_search_chunks(
    session: AsyncSession,
    *,
    library_ids: list[uuid.UUID] | None = None,
    q: str,
    limit: int,
    paper_ids: list[uuid.UUID] | None = None,
) -> list[tuple[PaperChunk, float]]:
    """关键词降级检索：问题分词后 ilike 命中任一词，按命中词数粗排。

    默认按 library_ids 限定关联库并集（chunk 去重）。传 paper_ids 时把检索限制在
    这些论文内。library_ids 为空/None 且给了 paper_ids 时走「纯 paper_ids、不 join
    库」分支（课题相关研究 / 个人库对话按论文集合直接检索）。
    """
    terms = [t for t in _WORD_RE.findall(q.lower()) if len(t) >= 2][:8]
    if not terms:
        return []
    cond = None
    for term in terms:
        clause = PaperChunk.text.ilike(f"%{term}%")
        cond = clause if cond is None else cond | clause
    if not library_ids and paper_ids:
        # 纯 paper_ids 分支：不 join library_papers，只按论文集合过滤
        stmt = select(PaperChunk).where(PaperChunk.paper_id.in_(paper_ids), cond)
    else:
        stmt = (
            select(PaperChunk)
            .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
            .where(LibraryPaper.library_id.in_(library_ids or []), cond)
            .distinct()
        )
        if paper_ids:
            stmt = stmt.where(PaperChunk.paper_id.in_(paper_ids))
    candidates = (await session.execute(stmt.limit(limit * 5))).scalars().all()

    def score_of(chunk: PaperChunk) -> float:
        lowered = chunk.text.lower()
        return float(sum(1 for t in terms if t in lowered))

    ranked = sorted(candidates, key=score_of, reverse=True)[:limit]
    return [(c, score_of(c)) for c in ranked]
