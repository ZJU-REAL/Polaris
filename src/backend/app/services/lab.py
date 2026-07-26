"""实验室数据面板（/lab 概况页）业务逻辑（不 import fastapi）：

- 索引与内容统计：文献库 / 论文 / 概念 / 全文分段 / 向量覆盖；
- token 用量排行榜（全员可见，管理员可关）；
- 跨库图谱：把「我可见的库」整体交给图谱构建。

可见范围一律走 ``libraries.library_visible_to``：公共库全员可读，个人库只有创建者
和平台管理员看得到。所有计数都先收敛到可见库集合再算，普通用户不会看到别人个人库
的数字。跨库同一篇论文只算一次（按 ``LibraryPaper.paper_id`` 去重）。
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.llm_config import LLMUsage
from app.models.paper import Concept, Paper, PaperChunk
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services import graph as graph_service
from app.services.chunks import chunk_vector_search_supported
from app.services.concepts import library_concept_ids
from app.services.libraries import library_visible_to
from app.services.papers import PAPER_STATUS_GROUPS

# 排行榜是否对普通用户可见（管理员开关，默认开；管理员任何时候都能看）
LEADERBOARD_SETTING_KEY = "lab_leaderboard_enabled"
DEFAULT_LEADERBOARD_ENABLED = True


class LibraryNotVisibleError(Exception):
    """请求的库对当前用户不可见（或不存在）。"""


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one())


def _has_vector(session: AsyncSession, column):
    """「向量已建」判定。

    postgres 上是 pgvector 列，NULL 判定就够；sqlite 上是 JSON 列，SQLAlchemy 会把
    Python None 存成字面量 ``null``，只判 IS NOT NULL 会把没建向量的也算进来。
    """
    if session.get_bind().dialect.name == "postgresql":
        return column.is_not(None)
    return and_(column.is_not(None), cast(column, String) != "null")


# ---- 可见库集合 ----


async def visible_libraries(session: AsyncSession, user: User) -> list[DirectionLibrary]:
    """当前用户可见的全部方向库（口径同 /libraries 列表）。"""
    rows = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    return [lib for lib in rows if library_visible_to(lib, user)]


# ---- 索引与内容统计 ----


async def lab_stats(session: AsyncSession, user: User) -> dict[str, Any]:
    """实验室概况数字：库 / 论文 / 概念 / 全文分段 / 向量覆盖。"""
    libraries = await visible_libraries(session, user)
    library_ids = [lib.id for lib in libraries]
    public_count = sum(1 for lib in libraries if lib.is_public)

    # 内容池是全局的（每日新论文、个人库导入都落在这里），不按库可见性收敛
    pool_total = await _count(session, select(func.count()).select_from(Paper))

    members = compiled = concepts = 0
    papers_with_chunks = total_chunks = chunks_with_embedding = papers_with_embedding = 0
    if library_ids:
        in_library = LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"])
        members = await _count(
            session,
            select(func.count(func.distinct(LibraryPaper.paper_id))).where(
                LibraryPaper.library_id.in_(library_ids), in_library
            ),
        )
        compiled = await _count(
            session,
            select(func.count(func.distinct(LibraryPaper.paper_id))).where(
                LibraryPaper.library_id.in_(library_ids),
                LibraryPaper.status.in_(PAPER_STATUS_GROUPS["compiled_any"]),
            ),
        )
        concepts = await _count(
            session,
            select(func.count())
            .select_from(Concept)
            .where(Concept.id.in_(library_concept_ids(library_ids))),
        )
        # 库内论文 id 子查询：分段与向量统计都挂在它上面（同样天然去重）
        member_ids = (
            select(LibraryPaper.paper_id)
            .where(LibraryPaper.library_id.in_(library_ids), in_library)
            .distinct()
        )
        papers_with_chunks = await _count(
            session,
            select(func.count(func.distinct(PaperChunk.paper_id))).where(
                PaperChunk.paper_id.in_(member_ids)
            ),
        )
        total_chunks = await _count(
            session,
            select(func.count())
            .select_from(PaperChunk)
            .where(PaperChunk.paper_id.in_(member_ids)),
        )
        chunks_with_embedding = await _count(
            session,
            select(func.count())
            .select_from(PaperChunk)
            .where(
                PaperChunk.paper_id.in_(member_ids), _has_vector(session, PaperChunk.embedding)
            ),
        )
        papers_with_embedding = await _count(
            session,
            select(func.count())
            .select_from(Paper)
            .where(Paper.id.in_(member_ids), _has_vector(session, Paper.embedding)),
        )

    return {
        "libraries": {
            "total": len(libraries),
            "public": public_count,
            "personal": len(libraries) - public_count,
        },
        "papers": {
            "pool_total": pool_total,
            "library_members_deduped": members,
            "compiled": compiled,
        },
        "concepts": {"total": concepts},
        "chunks": {
            "papers_with_chunks": papers_with_chunks,
            "total_chunks": total_chunks,
            "chunks_with_embedding": chunks_with_embedding,
            # sqlite 下向量存了也检索不了，前端据此区分「已嵌入」和「能按语义搜」
            "vector_search_supported": chunk_vector_search_supported(session),
        },
        "vectors": {"papers_with_embedding": papers_with_embedding, "papers_total": members},
        "leaderboard_enabled": await get_leaderboard_enabled(session),
    }


# ---- 用量排行榜 ----


async def get_leaderboard_enabled(session: AsyncSession) -> bool:
    row = await session.get(SystemSetting, LEADERBOARD_SETTING_KEY)
    return bool(row.value) if row is not None else DEFAULT_LEADERBOARD_ENABLED


async def set_leaderboard_enabled(session: AsyncSession, enabled: bool) -> bool:
    row = await session.get(SystemSetting, LEADERBOARD_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=LEADERBOARD_SETTING_KEY, value=enabled))
    else:
        row.value = enabled
    await session.commit()
    return enabled


async def usage_leaderboard(
    session: AsyncSession, *, days: int = 30, limit: int = 10
) -> list[dict[str, Any]]:
    """最近 N 天 token 消耗最多的前 limit 位成员（无记账记录的用户不出现）。"""
    since = utcnow() - timedelta(days=days)
    usage_sq = (
        select(
            LLMUsage.user_id.label("uid"),
            func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens).label("tokens"),
        )
        .where(LLMUsage.created_at >= since, LLMUsage.user_id.is_not(None))
        .group_by(LLMUsage.user_id)
        .subquery()
    )
    stmt = (
        select(User, usage_sq.c.tokens)
        .join(usage_sq, usage_sq.c.uid == User.id)
        .order_by(usage_sq.c.tokens.desc(), User.created_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "user_id": u.id,
            "display_name": u.display_name,
            "username": u.username,
            "has_avatar": u.has_avatar,
            "role": u.role,
            "tokens_used": int(tokens or 0),
        }
        for u, tokens in rows
    ]


# ---- 跨库图谱 ----


async def lab_graph(
    session: AsyncSession, *, user: User, library_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """全部可见库的并集图谱；给了 library_id 就只看那一个库（不可见则报错）。"""
    library_ids = [lib.id for lib in await visible_libraries(session, user)]
    if library_id is not None:
        if library_id not in library_ids:
            raise LibraryNotVisibleError(str(library_id))
        library_ids = [library_id]
    return await graph_service.libraries_graph(session, library_ids=library_ids)
