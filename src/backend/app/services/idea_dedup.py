"""想法的语义去重：向量余弦过阈值，再用重排模型复核。

想法生成（forge.dedup）和假设探索写想法（discovery.write_ideas）共用这一份：新想法
先和课题里已有的想法比，再和同一批里已经留下的比。两处用同一个阈值、同一种文本
（标题 + 一句话概述），所以谁先写的想法都能挡住另一边的近似重复。

比较对象不含已淘汰（rejected）和回收站里的想法：用户把一个想法扔掉，正是不想再
看到它，结果它却留在去重基准里把新生成的近似想法判成重复——扔得越多越生不出
东西，而且没有任何提示。
"""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.models.idea import Idea
from app.services.embedding import embed_documents, idea_vectors, upsert_idea_vector

DEFAULT_THRESHOLD = 0.85  # 余弦相似度超过它才进入复核
RERANK_CONFIRM_SCORE = 0.5  # 余弦超阈后重排复核的确认线


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)


def idea_text(title: str, summary: str | None) -> str:
    """去重与入库向量用的文本：标题 + 一句话概述。"""
    return f"{title}\n{summary or ''}"[:2000]


@dataclass(slots=True)
class DedupEntry:
    """去重基准里的一条：库内已有的想法，或本批里已经留下的候选。"""

    label: str  # 给人看的名字，写进「与谁重复」
    text: str
    vector: list[float]
    idea_id: uuid.UUID | None = None  # 库内想法才有


@dataclass(slots=True)
class DuplicateMatch:
    entry: DedupEntry
    cosine: float
    rerank_score: float


async def existing_idea_entries(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    space: EmbeddingSpace,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> list[DedupEntry]:
    """课题里参与去重的已有想法（未淘汰、不在回收站），带它们在 ``space`` 下的向量。

    这个空间下还没有向量的现场补嵌并落库（会提交）。补嵌失败就不比那几条——
    去重是尽力而为，不该因为它让调用方整步失败。
    """
    existing = (
        (
            await session.execute(
                select(Idea).where(
                    Idea.project_id == project_id,
                    Idea.status != "rejected",
                    Idea.trashed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    known = await idea_vectors(session, [i.id for i in existing], space)
    pending = [i for i in existing if i.id not in known]
    if pending:
        try:
            vectors, _ = await embed_documents(
                session,
                [idea_text(i.title, i.summary) for i in pending],
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
            )
            for idea, vector in zip(pending, vectors, strict=True):
                await upsert_idea_vector(session, idea.id, vector, space)
                known[idea.id] = vector
            await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 既有想法补嵌失败则跳过比对
            await session.rollback()
    return [
        DedupEntry(
            label=f"库内想法「{i.title}」",
            text=idea_text(i.title, i.summary),
            vector=known[i.id],
            idea_id=i.id,
        )
        for i in existing
        if i.id in known
    ]


async def confirmed_duplicate(
    llm: Any,
    text: str,
    other_text: str,
    cosine: float,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[bool, float]:
    """余弦超阈的一对用重排模型复核；重排不可用或出错时以余弦为准。"""
    try:
        ranked = await llm.rerank(
            text,
            [other_text],
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )
        score = float(ranked[0][1]) if ranked else 0.0
        return score >= RERANK_CONFIRM_SCORE, score
    except NotImplementedError:
        return True, cosine
    except Exception:  # noqa: BLE001 — rerank 异常降级为余弦判定
        return True, cosine


async def find_duplicate(
    llm: Any,
    *,
    text: str,
    vector: list[float],
    kept: list[DedupEntry],
    threshold: float = DEFAULT_THRESHOLD,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> DuplicateMatch | None:
    """``text`` 是否与 ``kept`` 里某一条重复；返回第一条确认重复的，没有返回 None。"""
    for entry in kept:
        cosine = cosine_similarity(vector, entry.vector)
        if cosine <= threshold:
            continue
        is_dup, rerank_score = await confirmed_duplicate(
            llm,
            text,
            entry.text,
            cosine,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )
        if is_dup:
            return DuplicateMatch(entry=entry, cosine=cosine, rerank_score=rerank_score)
    return None


__all__ = [
    "DEFAULT_THRESHOLD",
    "RERANK_CONFIRM_SCORE",
    "DedupEntry",
    "DuplicateMatch",
    "confirmed_duplicate",
    "cosine_similarity",
    "existing_idea_entries",
    "find_duplicate",
    "idea_text",
]
