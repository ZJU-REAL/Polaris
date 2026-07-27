"""测试里读写向量的小工具（向量不在主表上，见 app/models/vectors.py）。

fake provider 的向量是 1024 维（core/llm/fake.py），所以测试里手工造的向量默认
也用这个维度和这个模型名，才能和走真实写入路径建出来的向量落在同一个空间里。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace, active_space, set_active_space
from app.core.llm.fake import EMBEDDING_DIM
from app.models.vectors import IdeaVector, PaperChunkVector, PaperVector
from app.services.embedding import (
    upsert_chunk_vector,
    upsert_idea_vector,
    upsert_paper_vector,
)

# fake provider 走的是 "fake-default" 路由（router._FALLBACK_ROUTE）
FAKE_SPACE = EmbeddingSpace(model="fake-default", dim=EMBEDDING_DIM)


async def ensure_space(session: AsyncSession, space: EmbeddingSpace = FAKE_SPACE):
    """确保有激活空间（手工造向量时要先有它，否则检索侧无从过滤）。"""
    current = await active_space(session)
    if current is None:
        await set_active_space(session, space)
        await session.commit()
        return space
    return current


async def set_paper_vector(
    session: AsyncSession,
    paper_id: uuid.UUID,
    vector: list[float] | None = None,
    *,
    space: EmbeddingSpace = FAKE_SPACE,
) -> None:
    await ensure_space(session, space)
    await upsert_paper_vector(session, paper_id, vector or [0.0] * space.dim, space)
    await session.commit()


async def set_chunk_vector(
    session: AsyncSession,
    chunk_id: uuid.UUID,
    vector: list[float] | None = None,
    *,
    space: EmbeddingSpace = FAKE_SPACE,
) -> None:
    await ensure_space(session, space)
    await upsert_chunk_vector(session, chunk_id, vector or [0.0] * space.dim, space)
    await session.commit()


async def set_idea_vector(
    session: AsyncSession,
    idea_id: uuid.UUID,
    vector: list[float] | None = None,
    *,
    space: EmbeddingSpace = FAKE_SPACE,
) -> None:
    await ensure_space(session, space)
    await upsert_idea_vector(session, idea_id, vector or [0.0] * space.dim, space)
    await session.commit()


async def get_paper_vector(
    session: AsyncSession, paper_id: uuid.UUID
) -> list[float] | None:
    """该论文在激活空间下的向量（没有则 None）。"""
    space = await active_space(session)
    if space is None:
        return None
    row = (
        await session.execute(
            select(PaperVector.embedding).where(
                PaperVector.paper_id == paper_id, PaperVector.space == space.key
            )
        )
    ).scalar_one_or_none()
    return list(row) if row is not None else None


async def get_chunk_vector(
    session: AsyncSession, chunk_id: uuid.UUID
) -> list[float] | None:
    space = await active_space(session)
    if space is None:
        return None
    row = (
        await session.execute(
            select(PaperChunkVector.embedding).where(
                PaperChunkVector.chunk_id == chunk_id,
                PaperChunkVector.space == space.key,
            )
        )
    ).scalar_one_or_none()
    return list(row) if row is not None else None


async def get_idea_vector(
    session: AsyncSession, idea_id: uuid.UUID
) -> list[float] | None:
    space = await active_space(session)
    if space is None:
        return None
    row = (
        await session.execute(
            select(IdeaVector.embedding).where(
                IdeaVector.idea_id == idea_id, IdeaVector.space == space.key
            )
        )
    ).scalar_one_or_none()
    return list(row) if row is not None else None


async def count_paper_vectors(session: AsyncSession) -> int:
    rows = (await session.execute(select(PaperVector.paper_id))).scalars().all()
    return len(rows)
