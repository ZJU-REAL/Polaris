"""概念库路由（docs/api-m2.md §2）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import CONCEPT_STATUS_ACTIVE, Concept, paper_concepts
from app.models.user import User
from app.schemas.paper import (
    ConceptDetail,
    ConceptPaperRead,
    ConceptRead,
    ConceptRelatedRead,
    ConceptRelinkResult,
)
from app.services import concepts as concepts_service
from app.services import libraries as libraries_service
from app.services.libraries import get_library_for_project, get_source_library_ids

router = APIRouter(tags=["concepts"])


def _concept_read(
    concept: Concept,
    paper_count: int,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID | None = None,
) -> ConceptRead:
    """概念出参。

    概念本身不属于任何库（全平台一份）；project_id / library_id 是**本次访问的作用域**
    ——列表按调用方的课题/库回填，详情按「用到这个概念的论文在哪个库」推导，供前端
    「点进去回哪个库」用，都可能为空。"""
    return ConceptRead(
        id=concept.id,
        project_id=project_id,
        library_id=library_id,
        name=concept.name,
        category=concept.category,
        definition=concept.definition,
        paper_count=paper_count,
    )


@router.get("/concepts", response_model=list[ConceptRead])
async def lookup_concepts(
    name: str = Query(min_length=1, description="概念名（精确匹配，忽略大小写/连接符差异）"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConceptRead]:
    """平台级按名字查概念（不带库作用域）。

    池级上下文（每日推送 / 个人库 / 阅读页）的 [[双链]] 用它解析：概念全平台唯一，
    命中至多一条；空结果 = 这个概念还没入库。作用域字段一律为空（本就没有库语境）。
    """
    concepts = await concepts_service.find_by_name(session, name)
    return [
        _concept_read(c, await concepts_service.paper_count_of(session, c.id), None)
        for c in concepts
    ]


@router.get("/projects/{project_id}/concepts", response_model=list[ConceptRead])
async def list_concepts(
    project_id: uuid.UUID,
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConceptRead]:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    library_ids = await get_source_library_ids(session, project_id)
    rows = await concepts_service.list_concepts(
        session, library_ids=library_ids, category=category, q=q
    )
    return [_concept_read(concept, count, project_id) for concept, count in rows]


@router.post("/projects/{project_id}/concepts/relink", response_model=ConceptRelinkResult)
async def relink_concepts(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptRelinkResult:
    """全库概念补建：对已编译论文重抽 [[双链]]、建缺失概念并补齐关联（幂等）。

    面向历史数据（编译过但概念上链步骤没跑到的论文）；新概念定义分批调 LLM，
    并回填此前留下的占位概念（「…（定义待补充）」）重新拿定义，失败降级为占位、不阻塞。
    """
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    # 概念补建是「某具体库」的维护写操作（计库预算），落在课题的起源库上；
    # 课题若无任何可解析的库（全部解绑）则无事可做，返回零结果。
    library = await get_library_for_project(session, project_id)
    if library is None:
        return ConceptRelinkResult(papers=0, concepts_created=0, links_created=0)
    stats, _papers = await concepts_service.link_all_paper_concepts(
        session,
        library_id=library.id,
        llm=get_llm_router(),
        user_id=user.id,
        project_id=project_id,
        backfill=True,
    )
    return ConceptRelinkResult(**stats)


@router.get("/concepts/{concept_id}", response_model=ConceptDetail)
async def get_concept(
    concept_id: uuid.UUID,
    library_id: uuid.UUID | None = Query(
        default=None, description="库作用域：只列该库内关联到这个概念的论文；不传=全平台"
    ),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptDetail:
    """概念详情（登录即可读；P5c 方向库全实验室可读）。

    词条本身（名称/定义/slug）永远是同一份，按库变化的只有「关联论文」：
    从库的上下文点进来（带 library_id）就只列该库里关联它的论文，从池级上下文
    （每日推送 / 个人库 / 直接访问）点进来就列全平台的。不属于任何库的概念照常打开。
    """
    concept = await session.get(Concept, concept_id)
    # 候选词条（还只有一篇论文用到）按「不存在」处理：它还不是正式概念，等第二篇论文
    # 引用它就自动转正出现，不需要人工干预
    if concept is None or concept.status != CONCEPT_STATUS_ACTIVE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONCEPT_NOT_FOUND")
    project_id: uuid.UUID | None = None
    scope_library_id = library_id  # 只在显式带库时过滤关联论文
    if library_id is not None:
        library = await libraries_service.get_library(session, library_id)
        if library is None or not libraries_service.library_visible_to(library, user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
        project_id = library.project_id
    else:
        # 没给作用域时给个落点库（用到它的论文所在、最早入库的那个），供前端跳回；
        # 概念只挂在池内论文（如每日推送）上时为空。关联论文仍是全平台范围。
        scope = (
            await session.execute(
                select(LibraryPaper.library_id, DirectionLibrary.project_id)
                .join(paper_concepts, paper_concepts.c.paper_id == LibraryPaper.paper_id)
                .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
                .where(paper_concepts.c.concept_id == concept_id)
                .order_by(LibraryPaper.created_at)
                .limit(1)
            )
        ).first()
        library_id, project_id = scope if scope is not None else (None, None)
    papers = await concepts_service.papers_of_concept(
        session, concept.id, library_id=scope_library_id
    )
    related = await concepts_service.related_concepts(session, concept)
    base = _concept_read(concept, len(papers), project_id, library_id)
    return ConceptDetail(
        **base.model_dump(),
        wiki_content=concept.wiki_content,
        papers=[ConceptPaperRead.model_validate(p) for p in papers],
        related=[ConceptRelatedRead.model_validate(c) for c, _ in related],
    )
