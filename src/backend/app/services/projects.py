"""项目业务逻辑（不 import fastapi）。"""

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# 稀疏 definition 缺 arxiv_categories 时的检索默认分类（actions_wiki 也用）
DEFAULT_ARXIV_CATEGORIES = ["cs.CL", "cs.AI", "cs.LG"]


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


async def _unique_slug(session: AsyncSession, base: str) -> str:
    slug = base
    while (await session.execute(select(Project.id).where(Project.slug == slug))).first():
        slug = f"{base}-{uuid.uuid4().hex[:6]}"
    return slug


def in_my_projects(project_id_col, user_id: uuid.UUID):
    """「这条记录所属的课题我够得着吗」——课题作用域读取口的统一条件。

    够得着 = 课题是我建的（owner_id）。成员机制已随个人化定位移除（#625）：
    单机档位只有一个用户，服务器档的多用户隔离只看归属，不再有「加人共享」。
    课题下的东西（想法/实验/稿件/闸门/书架论文……）一律用这一条判，口径必须一致。
    """
    return project_id_col.in_(select(Project.id).where(Project.owner_id == user_id))


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> Sequence[Project]:
    """列出用户够得着的课题（本人创建的）。"""
    stmt = (
        select(Project)
        .where(in_my_projects(Project.id, user_id))
        .order_by(Project.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def list_projects_page(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    query: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[Project], int]:
    """Paginate projects visible to a user for project discovery surfaces."""
    filters = [in_my_projects(Project.id, user_id)]
    if query:
        pattern = f"%{query}%"
        filters.append(or_(Project.name.ilike(pattern), Project.slug.ilike(pattern)))
    if status:
        filters.append(Project.status == status)

    total = await session.scalar(select(func.count()).select_from(Project).where(*filters))
    stmt = (
        select(Project)
        .where(*filters)
        .order_by(Project.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    projects = (await session.execute(stmt)).scalars().all()
    return projects, int(total or 0)


async def create_project(
    session: AsyncSession, owner_id: uuid.UUID, data: ProjectCreate
) -> Project:
    """建课题（归属即 ``owner_id``，成员表已随 #625 移除）。

    P9c：课题不再拥有库，也不拥有收录配置——不自动建隐式库、不写收录配置。
    只建 project（name + 一句话 statement 存入 ``project.statement`` 供课题语境
    提示）+ 按 ``source_library_ids`` 关联**已有**文献库（可为空，空=课题暂无语料，
    各消费端给空态）。文献库全部是独立创建、管理员审批的（P9b）。
    """
    slug = await _unique_slug(session, data.slug or slugify(data.name))
    statement = (data.statement or "").strip()
    project = Project(
        name=data.name,
        slug=slug,
        statement=statement or None,
        research_mode=data.research_mode,
        owner_id=owner_id,
    )
    session.add(project)
    await session.flush()
    if data.source_library_ids:
        from app.services.libraries import set_source_libraries

        await set_source_libraries(
            session, topic_id=project.id, library_ids=list(data.source_library_ids)
        )
    await session.commit()
    await session.refresh(project)
    return project


async def get_project(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project | None:
    """取项目；非本人创建视为不存在（返回 None，不泄露存在性）。

    列表看得到的，点进去必须打得开——这是 :func:`list_projects` 的单条镜像，
    两边一起改。课题作用域的读取口（想法/实验/闸门等）大多经由这里鉴权。
    """
    stmt = select(Project).where(
        Project.id == project_id, in_my_projects(Project.id, user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def can_manage_project(project: Project, user: User) -> bool:
    """PATCH / 删除权限：项目 owner。成员机制移除（#625）后可见性==归属，
    这里恒等于「够得着就能管」，留着是给 API 层一个显式的管理判据出口。"""
    return project.owner_id == user.id


async def update_project(session: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    if data.name is not None:
        project.name = data.name
    if data.statement is not None:
        project.statement = data.statement.strip() or None
    if data.status is not None:
        project.status = data.status
    if data.research_mode is not None:
        project.research_mode = data.research_mode
    await session.commit()
    await session.refresh(project)
    return project


async def delete_project(session: AsyncSession, project: Project) -> None:
    """删除项目；论文/概念/任务等子表靠 FK ondelete=CASCADE 一并清除。"""
    await session.delete(project)
    await session.commit()
