"""方向文献库解析与成员行工具（P4 过渡期：project 1:1 隐式库，不 import fastapi）。

API 形状不变（仍收 project_id），service 层经这里解析到 DirectionLibrary 后
用 LibraryPaper 承接论文归属与判断字段；新 project 建库时同步建隐式库
（services/projects.py），这里的 get-or-create 只兜底历史/直插数据。
"""

import uuid
from typing import Any

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator, LibraryPaper
from app.models.paper import Concept, Paper
from app.models.project import Project, ProjectMember
from app.models.user import User


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
    """用户可管理论文（其所属方向的库 ∪ 被任命管理的库 ∪ 平台 admin 全库）
    的成员行：SELECT (Paper, LibraryPaper, project_id)。P6 起策展人/管理员与
    成员同权（docs-dev/workspace-ia-redesign.md §5）。"""
    my_projects = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    my_curated = select(DirectionLibraryCurator.library_id).where(
        DirectionLibraryCurator.user_id == user_id
    )
    is_admin = select(User.id).where(User.id == user_id, User.role == "admin").exists()
    return (
        select(Paper, LibraryPaper, DirectionLibrary.project_id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .where(
            or_(
                DirectionLibrary.project_id.in_(my_projects),
                DirectionLibrary.id.in_(my_curated),
                is_admin,
            )
        )
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
    can_manage: bool,
    paper_count: int,
    concept_count: int,
    last_compiled_at: Any,
) -> dict[str, Any]:
    return {
        "id": library.id,
        "name": library.name,
        "statement": library.statement,
        "cadence": library.cadence,
        "monthly_budget": library.monthly_budget,
        "project_id": library.project_id,
        "is_mine": library.project_id is not None and library.project_id in my_projects,
        "can_manage": can_manage,
        "paper_count": paper_count,
        "concept_count": concept_count,
        "last_compiled_at": last_compiled_at,
        "last_synced_at": _last_synced_of(library.ingest_state),
        "created_at": library.created_at,
        "updated_at": library.updated_at,
    }


async def list_libraries_overview(session: AsyncSession, *, user: User) -> list[dict[str, Any]]:
    """全部方向库 + 概要统计（读操作对所有登录用户开放，不做成员校验）。"""
    libraries = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    paper_counts, last_compiled, concept_counts = await _library_stats(
        session, [lib.id for lib in libraries]
    )
    my_projects = await _my_project_ids(session, user.id)
    my_curated = await _my_curated_library_ids(session, user.id)
    return [
        _overview_dict(
            lib,
            my_projects=my_projects,
            can_manage=(
                user.role == "admin"
                or lib.id in my_curated
                or (lib.project_id is not None and lib.project_id in my_projects)
            ),
            paper_count=paper_counts.get(lib.id, 0),
            concept_count=concept_counts.get(lib.id, 0),
            last_compiled_at=last_compiled.get(lib.id),
        )
        for lib in libraries
    ]


async def library_overview(
    session: AsyncSession, *, library: DirectionLibrary, user: User
) -> dict[str, Any]:
    """单库详情概要（同列表口径）。"""
    paper_counts, last_compiled, concept_counts = await _library_stats(session, [library.id])
    my_projects = await _my_project_ids(session, user.id)
    return _overview_dict(
        library,
        my_projects=my_projects,
        can_manage=await can_manage_library(session, user=user, library=library),
        paper_count=paper_counts.get(library.id, 0),
        concept_count=concept_counts.get(library.id, 0),
        last_compiled_at=last_compiled.get(library.id),
    )


# ---- P6 治理：策展人（界面叫「文献库管理员」）与库级写权限 ----


async def _my_curated_library_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(DirectionLibraryCurator.library_id).where(
            DirectionLibraryCurator.user_id == user_id
        )
    )
    return set(rows.scalars().all())


async def is_library_curator(
    session: AsyncSession, *, library_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(DirectionLibraryCurator.user_id).where(
            DirectionLibraryCurator.library_id == library_id,
            DirectionLibraryCurator.user_id == user_id,
        )
    )
    return row.first() is not None


async def _is_project_member(
    session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    return row.first() is not None


async def can_manage_library(
    session: AsyncSession, *, user: User, library: DirectionLibrary
) -> bool:
    """库级写权限（docs-dev/workspace-ia-redesign.md §5）：
    背后课题成员 ∪ 策展人（direction_library_curators）∪ 平台 admin。"""
    if user.role == "admin":
        return True
    if library.project_id is not None and await _is_project_member(
        session, project_id=library.project_id, user_id=user.id
    ):
        return True
    return await is_library_curator(session, library_id=library.id, user_id=user.id)


async def get_managed_project(
    session: AsyncSession, *, project_id: uuid.UUID, user: User
) -> Project | None:
    """库管理入口的统一鉴权（project 作用域的文献管理端点用）：课题成员照常放行；
    平台 admin 与该课题隐式库的策展人同权；无权限视为不存在（返回 None）。"""
    project = await session.get(Project, project_id)
    if project is None:
        return None
    if user.role == "admin":
        return project
    if await _is_project_member(session, project_id=project_id, user_id=user.id):
        return project
    library = (
        await session.execute(
            select(DirectionLibrary).where(DirectionLibrary.project_id == project_id)
        )
    ).scalar_one_or_none()
    if library is not None and await is_library_curator(
        session, library_id=library.id, user_id=user.id
    ):
        return project
    return None


async def list_curators(session: AsyncSession, library_id: uuid.UUID) -> list[dict[str, Any]]:
    stmt = (
        select(DirectionLibraryCurator.user_id, User.email, User.display_name)
        .join(User, User.id == DirectionLibraryCurator.user_id)
        .where(DirectionLibraryCurator.library_id == library_id)
        .order_by(DirectionLibraryCurator.created_at)
    )
    return [
        {"user_id": user_id, "email": email, "display_name": display_name}
        for user_id, email, display_name in (await session.execute(stmt)).all()
    ]


async def set_curators(
    session: AsyncSession, *, library: DirectionLibrary, user_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """全量替换策展人名单（平台 admin 专用）；未知 user_id 抛 ValueError。commit 落库。"""
    unique_ids = list(dict.fromkeys(user_ids))
    if unique_ids:
        found = set(
            (await session.execute(select(User.id).where(User.id.in_(unique_ids)))).scalars().all()
        )
        missing = [str(uid) for uid in unique_ids if uid not in found]
        if missing:
            raise ValueError(f"unknown user ids: {', '.join(missing)}")
    await session.execute(
        delete(DirectionLibraryCurator).where(DirectionLibraryCurator.library_id == library.id)
    )
    for uid in unique_ids:
        session.add(DirectionLibraryCurator(library_id=library.id, user_id=uid))
    await session.commit()
    return await list_curators(session, library.id)


# 库定义可编辑字段 → project.definition 写回键（库为权威，写时同步保持 ingest 兼容：
# search/snowball/score/watermark 仍从 project.definition / ingest_state 取数）
_DEFINITION_SYNC_KEYS = {
    "statement": "statement",
    "rubric": "rubric",
    "anchors": "anchor_papers",
    "cadence": "cadence",
}


async def update_library(
    session: AsyncSession, *, library: DirectionLibrary, fields: dict[str, Any]
) -> DirectionLibrary:
    """编辑库定义（name/statement/cadence/monthly_budget/rubric/anchors，显式传 null 可清空）。

    过渡期隐式库（project_id 非空）双源并存：ingest 读 project.definition——
    这里以库为权威，写入时把 statement/rubric/anchors/cadence 同步回
    project.definition 对应键（name 同步 project.name），两边不再漂移。
    """
    for key, value in fields.items():
        if key == "name" and not value:
            continue  # name 非空约束：显式 null/空串视为不改名
        setattr(library, key, value)
    if library.project_id is not None:
        project = await session.get(Project, library.project_id)
        if project is not None:
            if fields.get("name"):
                project.name = fields["name"]
            sync = {k: v for k, v in fields.items() if k in _DEFINITION_SYNC_KEYS}
            if sync:
                definition = (
                    dict(project.definition) if isinstance(project.definition, dict) else {}
                )
                for key, value in sync.items():
                    definition[_DEFINITION_SYNC_KEYS[key]] = value
                project.definition = definition
    await session.commit()
    await session.refresh(library)
    return library
