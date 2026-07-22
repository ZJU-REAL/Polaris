"""方向文献库解析与成员行工具（P4 过渡期：project 1:1 隐式库，不 import fastapi）。

API 形状不变（仍收 project_id），service 层经这里解析到 DirectionLibrary 后
用 LibraryPaper 承接论文归属与判断字段；新 project 建库时同步建隐式库
（services/projects.py），这里的 get-or-create 只兜底历史/直插数据。
"""

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Concept, Paper
from app.models.project import Project, ProjectMember


def implicit_library_for(project: Project) -> DirectionLibrary:
    """按 project 组一个隐式库对象（继承 definition 的 statement/rubric/anchors/cadence）。"""
    definition = project.definition if isinstance(project.definition, dict) else {}
    return DirectionLibrary(
        name=project.name,
        statement=definition.get("statement"),
        rubric=definition.get("rubric"),
        anchors=definition.get("anchor_papers"),
        ingest_state=project.ingest_state,
        cadence=definition.get("cadence"),
        created_by=project.owner_id,
        project_id=project.id,
    )


async def get_library_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> DirectionLibrary:
    """取 project 的隐式方向库；缺失时就地补建（flush 不 commit，随调用方事务落库）。"""
    stmt = select(DirectionLibrary).where(DirectionLibrary.project_id == project_id)
    library = (await session.execute(stmt)).scalar_one_or_none()
    if library is not None:
        return library
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")
    library = implicit_library_for(project)
    session.add(library)
    await session.flush()
    return library


async def get_library_id_for_project(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    return (await get_library_for_project(session, project_id)).id


async def get_membership(
    session: AsyncSession, *, library_id: uuid.UUID, paper_id: uuid.UUID
) -> LibraryPaper | None:
    stmt = select(LibraryPaper).where(
        LibraryPaper.library_id == library_id, LibraryPaper.paper_id == paper_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def ensure_membership(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    status: str = "candidate",
    **fields: Any,
) -> tuple[LibraryPaper, bool]:
    """成员行 get-or-create（flush 不 commit），返回 (行, 是否新建)。"""
    membership = await get_membership(session, library_id=library_id, paper_id=paper_id)
    if membership is not None:
        return membership, False
    membership = LibraryPaper(library_id=library_id, paper_id=paper_id, status=status, **fields)
    session.add(membership)
    await session.flush()
    return membership, True


async def membership_for_project(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> LibraryPaper | None:
    """按 project 解析隐式库后取成员行（工具层「论文是否在本方向库内」的统一检查）。"""
    library = await get_library_for_project(session, project_id)
    return await get_membership(session, library_id=library.id, paper_id=paper_id)


async def find_pool_paper(
    session: AsyncSession,
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    dedup_key: str | None = None,
) -> Paper | None:
    """按 arxiv → doi → dedup_key 优先级查全局内容池（写路径「先查池」的统一入口）。"""
    if arxiv_id:
        stmt = select(Paper).where(Paper.arxiv_id == arxiv_id).limit(1)
        if (paper := (await session.execute(stmt)).scalars().first()) is not None:
            return paper
    if doi:
        stmt = select(Paper).where(func.lower(Paper.doi) == doi.lower()).limit(1)
        if (paper := (await session.execute(stmt)).scalars().first()) is not None:
            return paper
    if dedup_key:
        stmt = select(Paper).where(Paper.dedup_key == dedup_key).limit(1)
        return (await session.execute(stmt)).scalars().first()
    return None


def member_paper_stmt(library_id: uuid.UUID) -> Select:
    """库内论文基础查询：SELECT (Paper, LibraryPaper) 按成员表过滤。"""
    return (
        select(Paper, LibraryPaper)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(LibraryPaper.library_id == library_id)
    )


def user_visible_paper_stmt(user_id: uuid.UUID) -> Select:
    """用户可见论文（其任一所属方向的库里有成员行）：SELECT (Paper, LibraryPaper, project_id)。"""
    return (
        select(Paper, LibraryPaper, DirectionLibrary.project_id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .join(ProjectMember, ProjectMember.project_id == DirectionLibrary.project_id)
        .where(ProjectMember.user_id == user_id)
    )


# ---- 共享方向库读视图（P5c：全实验室可读，docs-dev/workspace-ia-redesign.md §2/§5） ----


def _last_synced_of(ingest_state: Any) -> Any:
    """从 ingest_state 提取「上次同步时间」：优先 last_run.finished_at，退回 watermark。"""
    if not isinstance(ingest_state, dict):
        return None
    last_run = ingest_state.get("last_run")
    if isinstance(last_run, dict) and last_run.get("finished_at"):
        return last_run["finished_at"]
    return ingest_state.get("watermark")


async def get_library(session: AsyncSession, library_id: uuid.UUID) -> DirectionLibrary | None:
    return await session.get(DirectionLibrary, library_id)


async def _library_stats(
    session: AsyncSession, library_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, Any], dict[uuid.UUID, int]]:
    """批量聚合库统计：(库内论文数, 最近编译时间, 概念数)。

    论文数口径 = 相关性达标及之后（与论文列表的 library 组别名一致）。
    """
    from app.services.papers import PAPER_STATUS_GROUPS  # 延迟导入避免循环依赖

    if not library_ids:
        return {}, {}, {}
    paper_rows = await session.execute(
        select(LibraryPaper.library_id, func.count(), func.max(LibraryPaper.compiled_at))
        .where(
            LibraryPaper.library_id.in_(library_ids),
            LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
        )
        .group_by(LibraryPaper.library_id)
    )
    paper_counts: dict[uuid.UUID, int] = {}
    last_compiled: dict[uuid.UUID, Any] = {}
    for lib_id, count, compiled_at in paper_rows.all():
        paper_counts[lib_id] = int(count)
        last_compiled[lib_id] = compiled_at
    concept_rows = await session.execute(
        select(Concept.library_id, func.count())
        .where(Concept.library_id.in_(library_ids))
        .group_by(Concept.library_id)
    )
    concept_counts = {lib_id: int(count) for lib_id, count in concept_rows.all()}
    return paper_counts, last_compiled, concept_counts


async def _my_project_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    )
    return set(rows.scalars().all())


def _overview_dict(
    library: DirectionLibrary,
    *,
    my_projects: set[uuid.UUID],
    paper_count: int,
    concept_count: int,
    last_compiled_at: Any,
) -> dict[str, Any]:
    return {
        "id": library.id,
        "name": library.name,
        "statement": library.statement,
        "cadence": library.cadence,
        "project_id": library.project_id,
        "is_mine": library.project_id is not None and library.project_id in my_projects,
        "paper_count": paper_count,
        "concept_count": concept_count,
        "last_compiled_at": last_compiled_at,
        "last_synced_at": _last_synced_of(library.ingest_state),
        "created_at": library.created_at,
        "updated_at": library.updated_at,
    }


async def list_libraries_overview(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[dict[str, Any]]:
    """全部方向库 + 概要统计（读操作对所有登录用户开放，不做成员校验）。"""
    libraries = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    paper_counts, last_compiled, concept_counts = await _library_stats(
        session, [lib.id for lib in libraries]
    )
    my_projects = await _my_project_ids(session, user_id)
    return [
        _overview_dict(
            lib,
            my_projects=my_projects,
            paper_count=paper_counts.get(lib.id, 0),
            concept_count=concept_counts.get(lib.id, 0),
            last_compiled_at=last_compiled.get(lib.id),
        )
        for lib in libraries
    ]


async def library_overview(
    session: AsyncSession, *, library: DirectionLibrary, user_id: uuid.UUID
) -> dict[str, Any]:
    """单库详情概要（同列表口径）。"""
    paper_counts, last_compiled, concept_counts = await _library_stats(session, [library.id])
    my_projects = await _my_project_ids(session, user_id)
    return _overview_dict(
        library,
        my_projects=my_projects,
        paper_count=paper_counts.get(library.id, 0),
        concept_count=concept_counts.get(library.id, 0),
        last_compiled_at=last_compiled.get(library.id),
    )
