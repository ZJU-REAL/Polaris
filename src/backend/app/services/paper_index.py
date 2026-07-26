"""单篇论文的索引状态查询与手动重建（不 import fastapi）。

论文有两种向量，各自可能缺席：
- **论文级向量**（``papers.embedding``）：标题+作者+摘要一条向量，语义检索/去重用；
- **分块向量**（``paper_chunks.embedding``）：分段各一条，文献对话检索用。

两者的构建时间与模型名记在 ``papers`` 的四个元信息列上（见 models/paper.py），
``embedding`` 列本身只有向量。存量数据这些列为空 → 前端按「未构建」显示，重建一次即补齐。

单篇级操作在请求线程内做即可（一篇论文至多百来段，与重编译同量级），不必进 worker。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import LLMRouter
from app.models.paper import Paper, PaperChunk
from app.services.chunks import (
    embed_pending_chunks_for_papers,
    index_paper_abstract,
    replace_chunks,
)
from app.services.paper_enrich import embed_paper, paper_embedding_enabled

logger = logging.getLogger(__name__)


class IndexRebuildFailedError(Exception):
    """请求的重建项全部失败（路由层转 502）。"""


@dataclass
class VectorStatus:
    """一种向量的状态：有没有、什么时候建的、用的哪个模型。"""

    built: bool
    built_at: datetime | None
    model: str | None


@dataclass
class PaperIndexStatus:
    paper_vector: VectorStatus
    chunk_vector: VectorStatus
    chunk_count: int
    embedded_chunk_count: int
    has_fulltext: bool
    chunk_source: str | None  # fulltext | abstract | None（还没有分段）


async def get_index_status(session: AsyncSession, paper: Paper) -> PaperIndexStatus:
    """查一篇论文的两种向量状态（两条聚合查询，不读向量本体）。"""
    row = (
        await session.execute(
            select(
                func.count(PaperChunk.id),
                func.count(PaperChunk.embedding),  # count(col) 不计 NULL
                func.max(PaperChunk.source),
            ).where(PaperChunk.paper_id == paper.id)
        )
    ).one()
    chunk_count, embedded_count, source = int(row[0]), int(row[1]), row[2]
    return PaperIndexStatus(
        paper_vector=VectorStatus(
            built=paper.embedding is not None,
            built_at=paper.embedding_at,
            model=paper.embedding_model,
        ),
        chunk_vector=VectorStatus(
            built=embedded_count > 0,
            built_at=paper.chunk_embedding_at,
            model=paper.chunk_embedding_model,
        ),
        chunk_count=chunk_count,
        embedded_chunk_count=embedded_count,
        has_fulltext=bool(paper.full_text_path),
        chunk_source=source,
    )


async def rebuild_index(
    session: AsyncSession,
    paper: Paper,
    *,
    llm: LLMRouter,
    targets: set[str],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> PaperIndexStatus:
    """手动重建这篇论文的向量，返回重建后的状态。调用方负责鉴权。

    ``targets`` ⊆ {"paper", "chunks"}：
    - paper：重算论文级向量（覆盖旧向量，无论有没有）；
    - chunks：重切分段（有全文按全文、无全文用标题+摘要）后把全部分段重新嵌入。

    「重建」是真重建：分段先删后建，向量整体覆盖，不做「只补缺失」的增量——用户点
    这个按钮就是因为怀疑现有索引不对。单项失败不影响另一项，两项都失败才向上抛。
    """
    errors: list[str] = []

    if "paper" in targets:
        if not await paper_embedding_enabled(session):
            errors.append("paper: 管理员已关闭论文级向量")
        else:
            try:
                await embed_paper(paper, user_id=user_id, project_id=project_id)
                await session.commit()
            except Exception as e:  # noqa: BLE001 — provider 不支持嵌入等：记下继续
                logger.warning("paper embedding rebuild failed for %s", paper.id, exc_info=True)
                await session.rollback()
                errors.append(f"paper: {type(e).__name__}: {e}")

    if "chunks" in targets:
        try:
            await _rebuild_chunks(session, paper)
            await session.commit()
            # 分段是新的，embedding 全空 → 这里等价于「整篇重嵌」。摘要兜底块不走嵌入
            # 接口，由 _rebuild_chunks 就地拷贝论文级向量（此刻刚重建过，是最新的）。
            _, embed_error = await embed_pending_chunks_for_papers(
                session,
                paper_ids=[paper.id],
                llm=llm,
                user_id=user_id,
                project_id=project_id,
            )
            if embed_error:
                errors.append(f"chunks: {embed_error}")
        except Exception as e:  # noqa: BLE001
            logger.warning("chunk index rebuild failed for %s", paper.id, exc_info=True)
            await session.rollback()
            errors.append(f"chunks: {type(e).__name__}: {e}")

    if errors and len(errors) == len(targets):
        raise IndexRebuildFailedError("；".join(errors))
    await session.refresh(paper)
    return await get_index_status(session, paper)


async def _rebuild_chunks(session: AsyncSession, paper: Paper) -> int:
    """强制重切分段：有全文按全文重切，没有全文重建摘要兜底块。

    两条路径都是「先删后建」（不走 ensure 的「已有块就跳过」），所以新分段的
    embedding 一律为空，紧接着的重新嵌入就等价于覆盖旧向量——否则只补缺失，
    点了重建却什么都没变。
    """
    from pathlib import Path

    if paper.full_text_path and Path(paper.full_text_path).exists():
        full_text = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
        return await replace_chunks(session, paper, full_text)
    return await index_paper_abstract(session, paper)
