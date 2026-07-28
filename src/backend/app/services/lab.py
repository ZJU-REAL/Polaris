"""实验室数据面板（/lab 概况页）业务逻辑（不 import fastapi）：

- 索引与内容统计：文献库 / 论文 / 概念 / 全文分段 / 向量覆盖；
- token 用量排行榜（全员可见，管理员可关）；
- 跨库图谱：把「我可见的库」整体交给图谱构建。

可见范围一律走 ``libraries.library_visible_to``：公共库全员可读，个人库只有创建者
和平台管理员看得到。所有计数都先收敛到可见库集合再算，普通用户不会看到别人个人库
的数字。跨库同一篇论文只算一次（按 ``LibraryPaper.paper_id`` 去重）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import active_space
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.llm_config import LLMUsage
from app.models.paper import Concept, Paper, PaperChunk
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.models.vectors import PaperChunkVector, PaperVector
from app.models.voyage import VoyageRun, VoyageStep
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


# 「向量已建」不再需要方言特判：向量在侧表里，有行即已建（embedding 列 NOT NULL）。


# ---- 可见库集合 ----


async def visible_libraries(session: AsyncSession, user: User) -> list[DirectionLibrary]:
    """当前用户可见的全部方向库（口径同 /libraries 列表）。"""
    rows = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    return [lib for lib in rows if library_visible_to(lib, user)]


# ---- 每日同步收录状态 ----


TERMINAL_RUN_STATUSES = ("done", "failed", "cancelled")

_INGEST_STEP_KEYS = {
    "wiki.search_candidates": "candidates",
    "wiki.score_relevance": "score",
    "wiki.compile": "compile",
}


async def daily_ingest_status(
    session: AsyncSession, user: User, *, since: datetime | None = None
) -> list[dict[str, Any]]:
    """各可见库最近一轮「从每日论文自动收录」的进展与结果。

    每日抓取跑完会给每个到期的库起一轮 wiki_ingest；这里把那一轮的漏斗数字摊开，
    让人在每日新论文那张卡上就能看出「今天扫了多少、收了多少、还在跑没有」，
    而不必逐个库点进任务详情。只列请求者看得见的库。
    """
    libraries = await visible_libraries(session, user)
    if not libraries:
        return []
    by_id = {lib.id: lib for lib in libraries}
    if since is None:
        now = datetime.now(UTC)
        since = datetime(now.year, now.month, now.day, tzinfo=UTC)

    runs = (
        (
            await session.execute(
                select(VoyageRun)
                .where(
                    VoyageRun.kind == "wiki_ingest",
                    VoyageRun.library_id.in_(list(by_id)),
                    VoyageRun.created_at >= since,
                )
                .order_by(VoyageRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # 一个库当天可能被手动多触发几次，只看最近那轮
    latest: dict[uuid.UUID, VoyageRun] = {}
    for run in runs:
        if run.library_id is not None and run.library_id not in latest:
            latest[run.library_id] = run
    if not latest:
        return []

    steps = (
        (
            await session.execute(
                select(VoyageStep).where(VoyageStep.run_id.in_([r.id for r in latest.values()]))
            )
        )
        .scalars()
        .all()
    )
    by_run: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    for step in steps:
        key = _INGEST_STEP_KEYS.get(step.action)
        if key is None:
            continue
        by_run.setdefault(step.run_id, {})[key] = {
            "status": step.status,
            "observation": step.observation or {},
        }

    items: list[dict[str, Any]] = []
    for library_id, run in latest.items():
        parts = by_run.get(run.id, {})
        candidates = parts.get("candidates", {}).get("observation", {})
        score = parts.get("score", {}).get("observation", {})
        compiled = parts.get("compile", {}).get("observation", {})
        succeeded = int(score.get("succeeded") or 0)
        rejected = int(score.get("excluded") or 0)
        items.append(
            {
                "library_id": library_id,
                "library_name": by_id[library_id].name,
                "is_public": by_id[library_id].is_public,
                "voyage_id": run.id,
                "status": run.status,
                "started_at": run.created_at,
                # VoyageRun 没有独立的完成时间，终态时 updated_at 就是它
                "finished_at": run.updated_at if run.status in TERMINAL_RUN_STATUSES else None,
                # 漏斗：池子里扫了多少 → 送进打分多少 → 收下多少 → 编译了多少
                "feed_total": int(candidates.get("feed_total") or 0),
                "candidates": int(candidates.get("inserted") or 0),
                "admitted": max(0, succeeded - rejected),
                "rejected": rejected,
                "compiled": int(compiled.get("succeeded") or 0),
                "step_status": {k: v["status"] for k, v in parts.items()},
            }
        )
    items.sort(key=lambda it: (-it["admitted"], it["library_name"]))
    return items


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
        # 覆盖率按**激活空间**算：换了嵌入模型后如实掉下来，等重建补回，
        # 而不是拿旧模型建的、检索已经用不上的向量充数。
        space = await active_space(session)
        chunks_with_embedding = (
            await _count(
                session,
                select(func.count())
                .select_from(PaperChunkVector)
                .join(PaperChunk, PaperChunk.id == PaperChunkVector.chunk_id)
                .where(
                    PaperChunk.paper_id.in_(member_ids),
                    PaperChunkVector.space == space.key,
                ),
            )
            if space is not None
            else 0
        )
        papers_with_embedding = (
            await _count(
                session,
                select(func.count())
                .select_from(PaperVector)
                .where(
                    PaperVector.paper_id.in_(member_ids),
                    PaperVector.space == space.key,
                ),
            )
            if space is not None
            else 0
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
