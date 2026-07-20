"""论文全文分段索引（文献知识底座，不 import fastapi）。

- 切分：确定性代码，按段落边界贪心打包到 ~CHUNK_TARGET_CHARS，超长段硬切带重叠；
- 嵌入：wiki.link_concepts 步骤批量补齐（provider 不支持时留空）；
- 检索：postgres 走 pgvector 余弦，其他方言 / 无向量时降级关键词打分。
支撑文献库对话（services/library_chat.py）与后续 idea 生成等知识服务。
"""

import json
import re
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import LLMRouter
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


async def replace_chunks(session: AsyncSession, paper: Paper, full_text: str) -> int:
    """重建一篇论文的分段（旧分段删除；embedding 留空待批量补齐）。调用方负责 commit。

    入库前再清洗一遍控制字符：磁盘上历史抽取的 txt 可能仍带 0x00，postgres UTF8 会拒绝。
    """
    from app.services.literature.pdf_extract import sanitize_text

    await session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper.id))
    chunks = split_text(sanitize_text(full_text))
    for seq, chunk_text in enumerate(chunks):
        session.add(
            PaperChunk(
                paper_id=paper.id,
                project_id=paper.project_id,
                seq=seq,
                text=chunk_text,
            )
        )
    return len(chunks)


async def index_paper_fulltext(session: AsyncSession, paper: Paper) -> int:
    """从 full_text_path 读全文并重建分段；无全文返回 0。调用方负责 commit。"""
    if not paper.full_text_path or not Path(paper.full_text_path).exists():
        return 0
    full_text = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
    return await replace_chunks(session, paper, full_text)


async def embed_pending_chunks(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    limit: int = 2000,
) -> tuple[int, str | None]:
    """批量补齐缺失的分段向量，返回 (成功条数, 错误说明|None)。"""
    pending = (
        (
            await session.execute(
                select(PaperChunk)
                .where(PaperChunk.project_id == project_id, PaperChunk.embedding.is_(None))
                .order_by(PaperChunk.created_at, PaperChunk.seq)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    embedded = 0
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        try:
            vectors = await llm.embed(
                [c.text[:2000] for c in batch],
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
            )
        except NotImplementedError:
            return embedded, "provider does not support embeddings"
        except Exception as e:  # noqa: BLE001 — 嵌入失败不影响主流程
            return embedded, f"{type(e).__name__}: {e}"
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.embedding = vector
            embedded += 1
        await session.commit()
    return embedded, None


# ---- 检索 ----


def chunk_vector_search_supported(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


async def semantic_search_chunks(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query_vector: list[float],
    limit: int,
    paper_ids: list[uuid.UUID] | None = None,
) -> list[tuple[PaperChunk, float]]:
    """pgvector 余弦检索（仅 postgres；调用方需先判 chunk_vector_search_supported）。

    传 paper_ids 时把检索限制在这些论文内（伴读引用指定文献用）。
    """
    qv = json.dumps(query_vector)
    params: dict[str, object] = {"qv": qv, "pid": str(project_id), "k": limit}
    paper_filter = ""
    if paper_ids:
        paper_filter = "AND paper_id = ANY(CAST(:pids AS uuid[])) "
        params["pids"] = [str(p) for p in paper_ids]
    rows = (
        await session.execute(
            sa_text(
                "SELECT id, 1 - (embedding <=> CAST(:qv AS vector)) AS score "
                "FROM paper_chunks "
                "WHERE project_id = :pid AND embedding IS NOT NULL "
                f"{paper_filter}"
                "ORDER BY embedding <=> CAST(:qv AS vector) "
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
    project_id: uuid.UUID,
    q: str,
    limit: int,
    paper_ids: list[uuid.UUID] | None = None,
) -> list[tuple[PaperChunk, float]]:
    """关键词降级检索：问题分词后 ilike 命中任一词，按命中词数粗排。

    传 paper_ids 时把检索限制在这些论文内。
    """
    terms = [t for t in _WORD_RE.findall(q.lower()) if len(t) >= 2][:8]
    if not terms:
        return []
    cond = None
    for term in terms:
        clause = PaperChunk.text.ilike(f"%{term}%")
        cond = clause if cond is None else cond | clause
    stmt = select(PaperChunk).where(PaperChunk.project_id == project_id, cond)
    if paper_ids:
        stmt = stmt.where(PaperChunk.paper_id.in_(paper_ids))
    candidates = (await session.execute(stmt.limit(limit * 5))).scalars().all()

    def score_of(chunk: PaperChunk) -> float:
        lowered = chunk.text.lower()
        return float(sum(1 for t in terms if t in lowered))

    ranked = sorted(candidates, key=score_of, reverse=True)[:limit]
    return [(c, score_of(c)) for c in ranked]
