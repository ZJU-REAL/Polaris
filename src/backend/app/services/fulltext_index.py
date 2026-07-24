"""可选全文索引：按论文集合批量抓 PDF → 分段 → 嵌入（文献对话检索底座）。

长任务，由 worker（worker/tasks.py::index_papers_fulltext_task）调用；单篇失败不打断，
无 arxiv 或下载失败的论文 best-effort 跳过。已建分段的论文不重建（避免丢弃已有向量）。
不 import fastapi。
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import LLMRouter
from app.models.paper import Paper, PaperChunk
from app.services.chunks import embed_pending_chunks_for_papers, index_paper_fulltext
from app.services.papers import (
    PdfFetchFailedError,
    PdfSourceUnsupportedError,
    fetch_pdf,
)

logger = logging.getLogger(__name__)


async def index_papers_fulltext(
    session: AsyncSession,
    *,
    paper_ids: list[uuid.UUID],
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
) -> dict[str, int | str | None]:
    """对给定论文集合建全文索引：抓 PDF（缺全文时）→ 分段 → 批量嵌入。

    - 已有分段的论文跳过（不重建，保留已有向量）。
    - 无 arxiv / 下载失败且本地也无全文 → 跳过，不抛。
    返回 {papers, indexed, embedded, skipped, embed_error}。
    """
    indexed = 0
    skipped = 0
    for pid in paper_ids:
        paper = await session.get(Paper, pid)
        if paper is None:
            skipped += 1
            continue
        # 已建分段的论文不重建（index_paper_fulltext 会删旧分段，会连带丢已有向量）
        existing = await session.scalar(
            select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == pid)
        )
        if existing:
            skipped += 1
            continue
        # 缺全文时 best-effort 抓 PDF + 抽全文（fetch_pdf 已有全文/PDF 时幂等直接返回）
        if not paper.full_text_path:
            try:
                paper = await fetch_pdf(session, paper, user_id=user_id)
            except (PdfSourceUnsupportedError, PdfFetchFailedError):
                skipped += 1
                continue
            except Exception:  # noqa: BLE001 — 单篇抓取异常不打断批处理
                logger.warning("fetch_pdf failed for paper %s", pid, exc_info=True)
                skipped += 1
                continue
        try:
            n = await index_paper_fulltext(session, paper)
        except Exception:  # noqa: BLE001 — 单篇分段异常不打断批处理
            logger.warning("index_paper_fulltext failed for paper %s", pid, exc_info=True)
            await session.rollback()
            skipped += 1
            continue
        if n:
            await session.commit()
            indexed += 1
        else:
            skipped += 1
    embedded, embed_error = await embed_pending_chunks_for_papers(
        session, paper_ids=paper_ids, llm=llm, user_id=user_id
    )
    return {
        "papers": len(paper_ids),
        "indexed": indexed,
        "embedded": embedded,
        "skipped": skipped,
        "embed_error": embed_error,
    }
