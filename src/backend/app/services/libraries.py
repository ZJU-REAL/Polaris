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
from app.models.paper import Paper
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
