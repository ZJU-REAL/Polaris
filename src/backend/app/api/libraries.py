"""共享方向库路由（P5c 只读 + P6 治理，docs-dev/workspace-ia-redesign.md §2/§5/§6/§7）。

方向库对全实验室可读：读端点只做登录校验、不做课题成员校验。
治理端点（库定义编辑 / 策展人任命）按库级写权限校验：成员 ∪ 策展人 ∪ 平台 admin
（策展人任命仅平台 admin）。批量写/管理入口（ingest、论文管理、概念补建等）仍走
project 作用域端点（鉴权同样接入库级写权限助手）。
个人文献库路由在 ``app/api/library.py``（/me/library），勿混淆。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    current_active_user,
    require_admin,
    require_llm_chat,
    require_llm_task,
)
from app.core.db import get_session
from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router
from app.core.queue import TaskQueue, get_task_queue
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.project import Project
from app.models.user import User
from app.schemas.graph import GraphResponse
from app.schemas.ingest import IngestRequest, IngestStateRead
from app.schemas.libraries import (
    CuratorRead,
    CuratorsUpdate,
    DirectionLibraryDetail,
    DirectionLibrarySummary,
    DirectionLibraryUpdate,
    DuplicateCandidateGroup,
    LibraryBudgetRead,
    LibraryCreate,
    LibraryReject,
    PaperMergeRequest,
    PaperMergeResult,
    SuggestDefinitionRequest,
    SuggestDefinitionResponse,
)
from app.schemas.note import NotebookPage, NoteWithPaper
from app.schemas.paper import (
    ConceptRead,
    PaperBatchIds,
    PaperChatRequest,
    PaperDetail,
    PaperListPage,
    PaperManualCreate,
    PaperRead,
    ScoredConcept,
    ScoredPaper,
    SearchResponse,
    TagRead,
)
from app.schemas.voyage import VoyageRead
from app.services import concepts as concepts_service
from app.services import graph as graph_service
from app.services import ingest as ingest_service
from app.services import libraries as libraries_service
from app.services import library_chat as library_chat_service
from app.services import notes as notes_service
from app.services import paper_import as paper_import_service
from app.services import paper_merge as paper_merge_service
from app.services import papers as papers_service
from app.services import relevance as relevance_service

router = APIRouter(tags=["libraries"])

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _paper_detail(
    session: AsyncSession, view: papers_service.PaperView, user_id: uuid.UUID
) -> PaperDetail:
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[view.id], user_id=user_id
    )
    return PaperDetail.model_validate(view).model_copy(update=extras[view.id])


async def _get_library(session: AsyncSession, library_id: uuid.UUID) -> DirectionLibrary:
    library = await libraries_service.get_library(session, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


async def _get_managed_library(
    session: AsyncSession, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    """治理端点统一入口：库存在 + 请求者有库级写权限（成员/策展人/admin），否则 403。"""
    library = await _get_library(session, library_id)
    if not await libraries_service.can_manage_library(session, user=user, library=library):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_MANAGE_FORBIDDEN")
    return library


async def _get_visible_library(
    session: AsyncSession, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    """只读端点统一入口：库存在 + 对请求者可见（P10：公共库全员，个人库仅归属人/admin）；
    不可见按不存在处理（404），避免个人库经 id 泄漏内容。"""
    library = await _get_library(session, library_id)
    if not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


async def _reads_with_extras(
    session: AsyncSession, papers: list, user_id: uuid.UUID
) -> list[PaperRead]:
    """ORM → schema，回填 tags/starred/reading_status/note_count（个人维度，全员可用）。"""
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[p.id for p in papers], user_id=user_id
    )
    return [PaperRead.model_validate(p).model_copy(update=extras[p.id]) for p in papers]


@router.get("/libraries", response_model=list[DirectionLibrarySummary])
async def list_libraries(
    type: str | None = Query(default=None, pattern="^(personal|public|all)$"),
    status_filter: str | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DirectionLibrarySummary]:
    """可见方向库（P10）：普通用户看自己的个人库 + 全部公共库，admin 看全部。

    可选 ``type``（personal|public|all，默认 all）与 ``status`` 在可见集合内进一步筛选。
    """
    rows = await libraries_service.list_libraries_overview(
        session, user=user, type=type, status=status_filter
    )
    return [DirectionLibrarySummary(**row) for row in rows]


@router.post(
    "/libraries", response_model=DirectionLibraryDetail, status_code=status.HTTP_201_CREATED
)
async def create_library(
    data: LibraryCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    """用户独立新建方向文献库（任意登录用户，P10）：新库即刻可用的**个人库**
    （status=active、is_public=false，仅创建者 + admin 可见，token 记创建者账），
    无需审批。创建者记为 submitted_by 并自动成为该库策展人。想转公共（全实验室可见、
    走系统 key）经 POST /libraries/{id}/request-public 申请、admin 审批。不属于任何课题。
    """
    library = await libraries_service.create_library(
        session,
        name=data.name,
        statement=data.statement,
        rubric=data.rubric,
        anchors=data.anchors,
        cadence=data.cadence,
        keywords=data.keywords,
        monthly_budget=data.monthly_budget,
        created_by=user.id,
    )
    await session.commit()
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/suggest-definition", response_model=SuggestDefinitionResponse)
async def suggest_library_definition(
    data: SuggestDefinitionRequest,
    user: User = Depends(require_llm_task),
) -> SuggestDefinitionResponse:
    """AI 根据研究方向名称 + 一句话描述生成一套收录设置（arXiv 分类/检索关键词/
    打分维度/锚点论文），供建库或编辑弹窗一键填表。

    不需要 library id。同步 LLM→JSON，生成失败/解析不出时返回结构完整的空兜底（各字段
    空列表）且仍 200，前端可回退手填。费用记个人账（需 full 大模型权限）。
    """
    suggestion = await libraries_service.suggest_definition(
        name=data.name,
        statement=data.statement,
        llm=get_llm_router(),
        user_id=user.id,
    )
    return SuggestDefinitionResponse(**suggestion)


@router.get("/libraries/{library_id}", response_model=DirectionLibraryDetail)
async def get_library(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    library = await _get_library(session, library_id)
    # 可见性（P9b）：pending/rejected 库仅创建者 + admin 可见，其余人视为不存在。
    if not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/{library_id}/request-public", response_model=DirectionLibraryDetail)
async def request_library_public(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    """申请把个人库转为公共库（P10）：创建者/策展人发起 → status=pending 等 admin 审批；
    **平台 admin 发起则直接通过**（直接转公共，无需审批）。无权者视为不存在（404）。
    """
    library = await _get_library(session, library_id)
    is_admin = user.role == "admin"
    is_owner = library.submitted_by is not None and library.submitted_by == user.id
    is_curator = await libraries_service.is_library_curator(
        session, library_id=library.id, user_id=user.id
    )
    if not (is_admin or is_owner or is_curator):
        # 能看到但无权发起 → 403；看不到（陌生人个人库）→ 404 隐藏存在。
        if libraries_service.library_visible_to(library, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_MANAGE_FORBIDDEN")
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    if is_admin:
        library = await libraries_service.approve_library(session, library=library)
    else:
        library = await libraries_service.request_public(session, library=library)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/{library_id}/cancel-request-public", response_model=DirectionLibraryDetail)
async def cancel_request_library_public(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    """撤回转公共申请（P10）：创建者/策展人把 pending 退回可用的个人库
    （is_public=false、status=active）。无权者视为不存在（404）。"""
    library = await _get_library(session, library_id)
    is_owner = library.submitted_by is not None and library.submitted_by == user.id
    is_curator = await libraries_service.is_library_curator(
        session, library_id=library.id, user_id=user.id
    )
    if not (is_owner or is_curator or user.role == "admin"):
        if libraries_service.library_visible_to(library, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_MANAGE_FORBIDDEN")
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    library = await libraries_service.cancel_request_public(session, library=library)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/{library_id}/make-personal", response_model=DirectionLibraryDetail)
async def make_library_personal(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> DirectionLibraryDetail:
    """把公共库转回个人库（平台 admin，P10）：is_public → false、status=active。
    转回后仅归属人 + admin 可见，其他成员看不到。"""
    library = await _get_library(session, library_id)
    library = await libraries_service.make_personal(session, library=library)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/{library_id}/approve", response_model=DirectionLibraryDetail)
async def approve_library(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> DirectionLibraryDetail:
    """审批通过转公共库（平台 admin，P10）：is_public → true、status → active，
    全实验室可见、ingest 走系统/全局 key。"""
    library = await _get_library(session, library_id)
    library = await libraries_service.approve_library(session, library=library)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.post("/libraries/{library_id}/reject", response_model=DirectionLibraryDetail)
async def reject_library(
    library_id: uuid.UUID,
    data: LibraryReject,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> DirectionLibraryDetail:
    """驳回转公共申请（平台 admin，P10）：退回可用的个人库（is_public=false、
    status=active），可带理由；创建者可调整后再申请。"""
    library = await _get_library(session, library_id)
    library = await libraries_service.reject_library(session, library=library, note=data.note)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.patch("/libraries/{library_id}", response_model=DirectionLibraryDetail)
async def update_library(
    library_id: uuid.UUID,
    data: DirectionLibraryUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    """编辑库定义（可管理者）：name/monthly_budget 与收录配置（statement/cadence/
    rubric/anchors/keywords/goals/scope/questions）。

    P8a：收录配置写入 library.definition（ingest 唯一权威源），不再写回起源课题。
    """
    library = await _get_managed_library(session, library_id, user)
    fields = data.model_dump(exclude_unset=True)
    if fields:
        library = await libraries_service.update_library(session, library=library, fields=fields)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.delete("/libraries/{library_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library(
    library_id: uuid.UUID,
    force: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删库（P10）：个人库创建者本人或 admin 可删；公共库仅 admin 可删（否则 403）。
    论文内容池行不动，成员行/概念/策展人一并清除。

    仍有课题关联时默认拒绝（409 LIBRARY_HAS_TOPICS），带 ``?force=true`` 才会
    一并解除关联（不影响课题本身，课题只是失去这条语料来源）。
    """
    library = await _get_library(session, library_id)
    try:
        await libraries_service.delete_library(
            session, library=library, user=user, force=force
        )
    except libraries_service.LibraryDeleteForbiddenError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_DELETE_FORBIDDEN") from e
    except libraries_service.LibraryHasTopicsError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LIBRARY_HAS_TOPICS") from e


@router.get("/libraries/{library_id}/budget", response_model=LibraryBudgetRead)
async def get_library_budget(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryBudgetRead:
    """本月预算消耗（可管理者）：库侧 LLM 调用（打分/编译/概念定义/向量化）的聚合。"""
    library = await _get_managed_library(session, library_id, user)
    usage = await ingest_service.monthly_library_usage(session, library.id)
    budget = library.monthly_budget
    used = int(usage["total_tokens"])
    return LibraryBudgetRead(
        month=usage["month"],
        monthly_budget=budget,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        used_tokens=used,
        remaining_tokens=None if not budget else max(0, int(budget) - used),
        exhausted=bool(budget) and used >= int(budget),
    )


@router.post(
    "/libraries/{library_id}/ingest/run",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_library_ingest(
    library_id: uuid.UUID,
    data: IngestRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    """对某个方向库直接触发抓取（P9a）：可管理者（成员/策展人/admin）皆可。

    管理员创建的独立库（project_id 为空）由此入口驱动 ingest；起源课题的隐式库同时
    带上 project 以兼容活动流/鉴权。互斥以库为准，超预算 409 拒绝。
    """
    library = await _get_managed_library(session, library_id, user)
    if library.status != "active":
        # 待审批 / 已驳回的库不能抓取（P9b：仅 active 且预算内可触发）。
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LIBRARY_NOT_ACTIVE")
    project = (
        await session.get(Project, library.project_id)
        if library.project_id is not None
        else None
    )
    try:
        run = await ingest_service.create_ingest_voyage(
            session,
            library=library,
            project=project,
            mode=data.mode,
            knobs=data.knobs,
            created_by=user.id,
        )
    except ingest_service.IngestConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="INGEST_ALREADY_RUNNING") from e
    except ingest_service.LibraryBudgetExhaustedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LIBRARY_BUDGET_EXHAUSTED") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/libraries/{library_id}/curators", response_model=list[CuratorRead])
async def list_curators(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[CuratorRead]:
    """策展人名单（界面叫「文献库管理员」）；可管理者可见。"""
    library = await _get_managed_library(session, library_id, user)
    rows = await libraries_service.list_curators(session, library.id)
    return [CuratorRead(**row) for row in rows]


@router.put("/libraries/{library_id}/curators", response_model=list[CuratorRead])
async def set_curators(
    library_id: uuid.UUID,
    data: CuratorsUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> list[CuratorRead]:
    """全量替换策展人名单（仅平台 admin）。"""
    library = await _get_library(session, library_id)
    try:
        rows = await libraries_service.set_curators(
            session, library=library, user_ids=data.user_ids
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [CuratorRead(**row) for row in rows]


@router.get("/libraries/{library_id}/papers", response_model=PaperListPage)
async def list_library_papers(
    library_id: uuid.UUID,
    status_filter: str | None = Query(default="library", alias="status"),
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    starred: bool | None = Query(default=None),
    reading_status: str | None = Query(default=None, pattern="^(unread|reading|read)$"),
    author: str | None = Query(default=None),
    affiliation: str | None = Query(default=None),
    published_from: datetime | None = Query(default=None),
    published_to: datetime | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    sort: str = Query(default="relevance", pattern="^(relevance|-published_at)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperListPage:
    """库内论文（分页/检索/排序/过滤）。缺省只列相关性达标的（status=library 组别名）。

    过滤参数与课题论文列表一致（星标/阅读状态/标签/作者/机构/发表与入库时间）；
    ``status=excluded`` 取该库垃圾桶。P9e 起标签是库作用域，独立库同样可用。
    """
    library = await _get_visible_library(session, library_id, user)
    items, total = await papers_service.list_papers(
        session,
        library_id=library.id,
        project_id=library.project_id,
        status=status_filter,
        q=q,
        tag=tag,
        starred=starred,
        reading_status=reading_status,
        author=author,
        affiliation=affiliation,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
        user_id=user.id,
        sort=sort,
        page=page,
        size=size,
    )
    return PaperListPage(
        items=await _reads_with_extras(session, list(items), user.id),
        total=total,
        page=page,
        size=size,
    )


@router.get("/libraries/{library_id}/concepts", response_model=list[ConceptRead])
async def list_library_concepts(
    library_id: uuid.UUID,
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConceptRead]:
    library = await _get_visible_library(session, library_id, user)
    rows = await concepts_service.list_concepts(
        session, library_ids=[library.id], category=category, q=q
    )
    return [
        ConceptRead(
            id=concept.id,
            project_id=library.project_id,
            name=concept.name,
            category=concept.category,
            definition=concept.definition,
            paper_count=count,
        )
        for concept, count in rows
    ]


@router.get("/libraries/{library_id}/search", response_model=SearchResponse)
async def search_library(
    library_id: uuid.UUID,
    q: str = Query(min_length=1),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchResponse:
    """库内检索（关键词/语义）。语义模式的 embed/rerank 记个人账（无课题上下文）。"""
    library = await _get_visible_library(session, library_id, user)

    mode_used = "keyword"
    reranked = False
    paper_rows: list = []
    if mode == "semantic" and papers_service.semantic_search_supported(session):
        try:
            vectors = await get_llm_router().embed([q], user_id=user.id)
            candidates = await papers_service.semantic_search_papers(
                session,
                library_id=library.id,
                project_id=library.project_id,
                query_vector=vectors[0],
                limit=max(papers_service.RERANK_CANDIDATES, limit),
            )
            mode_used = "semantic"
            paper_rows, reranked = await papers_service.rerank_paper_rows(
                get_llm_router(), query=q, rows=candidates, limit=limit, user_id=user.id
            )
        except NotImplementedError:
            mode_used = "keyword"  # embedding 路由的 provider 不支持 → 回退
    if mode_used == "keyword":
        paper_rows = await papers_service.keyword_search_papers(
            session,
            library_id=library.id,
            project_id=library.project_id,
            q=q,
            limit=limit,
            user_id=user.id,
        )
    concept_rows = await papers_service.keyword_search_concepts(
        session, library_id=library.id, q=q, limit=limit
    )

    concepts = []
    for concept, score in concept_rows:
        count = await concepts_service.paper_count_of(session, concept.id)
        concepts.append(
            ScoredConcept(
                id=concept.id,
                project_id=library.project_id,
                name=concept.name,
                category=concept.category,
                definition=concept.definition,
                paper_count=count,
                score=score,
            )
        )
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[p.id for p, _ in paper_rows], user_id=user.id
    )
    papers = [
        ScoredPaper(**(PaperRead.model_validate(p).model_dump() | extras[p.id]), score=s)
        for p, s in paper_rows
    ]
    return SearchResponse(papers=papers, concepts=concepts, mode_used=mode_used, reranked=reranked)


# ---- P9d 库级论文管理 / ingest 状态 / 图谱 / 对话 / 笔记（库工作台，含独立库） ----
#
# 说明：单篇写操作（改状态/软删召回/彻底删/重编译/标签）仍走 papers 路由的
# paper 级端点（经库可见性解析成员行，可管理者含策展人/创建者/admin）；本节只补
# 「集合级」库端点——课题版按 project 作用域，独立库靠这些端点获得同等管理能力。


@router.post(
    "/libraries/{library_id}/papers",
    response_model=PaperDetail,
    status_code=status.HTTP_201_CREATED,
)
async def add_library_paper_manually(
    library_id: uuid.UUID,
    data: PaperManualCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Any:
    """手动把一篇文献加进该库：arxiv_id / doi / bibtex 三选一（可管理者）。"""
    library = await _get_managed_library(session, library_id, user)
    try:
        paper = await paper_import_service.add_manual_paper_to_library(
            session,
            library=library,
            arxiv_id=data.arxiv_id,
            doi=data.doi,
            bibtex=data.bibtex,
            project_id=library.project_id,
        )
    except paper_import_service.DuplicatePaperError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "PAPER_EXISTS", "paper_id": str(e.paper_id)},
        )
    except paper_import_service.ParseFailedError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"PARSE_FAILED: {e}"
        ) from e
    paper_id, user_id, project_id = paper.id, user.id, library.project_id
    membership = await libraries_service.get_membership(
        session, library_id=library.id, paper_id=paper_id
    )
    if membership is not None:
        await relevance_service.score_added_paper_best_effort(
            session, paper, membership, project_id=project_id, user_id=user_id
        )
    view = await papers_service.get_library_paper_view(
        session,
        library_id=library.id,
        project_id=project_id,
        paper_id=paper_id,
        with_concepts=True,
    )
    return await _paper_detail(session, view, user_id)


@router.post("/libraries/{library_id}/papers/batch-delete")
async def batch_delete_library_papers(
    library_id: uuid.UUID,
    data: PaperBatchIds,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, int]:
    """批量删除库内论文（非本库的 id 忽略），返回 {deleted}。默认软删；hard=true 彻底删除。"""
    library = await _get_managed_library(session, library_id, user)
    deleted = await papers_service.delete_library_papers(
        session, library=library, paper_ids=data.paper_ids, hard=data.hard
    )
    return {"deleted": deleted}


@router.post("/libraries/{library_id}/trash/empty")
async def empty_library_trash(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, int]:
    """清空该库垃圾桶：彻底删除库内全部已删除论文成员行。"""
    library = await _get_managed_library(session, library_id, user)
    deleted = await papers_service.empty_library_trash(session, library=library)
    return {"deleted": deleted}


@router.get("/libraries/{library_id}/tags", response_model=list[TagRead])
async def list_library_tags(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[TagRead]:
    """库标签列表（含引用论文数）。"""
    library = await _get_visible_library(session, library_id, user)
    rows = await papers_service.list_library_tags(session, library_id=library.id)
    return [TagRead(**row) for row in rows]


@router.get("/libraries/{library_id}/ingest/state", response_model=IngestStateRead)
async def get_library_ingest_state(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> IngestStateRead:
    """该库的建库/同步状态：上次同步时间、抓取进度计数、在跑任务、下次自动同步（可管理者）。"""
    library = await _get_visible_library(session, library_id, user)
    state = await ingest_service.library_ingest_state(session, library)
    return IngestStateRead(**state)


@router.get("/libraries/{library_id}/graph", response_model=GraphResponse)
async def library_graph(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GraphResponse:
    """库知识图谱：论文 / 作者 / 概念节点与关联边（确定性构建，不走 LLM；全实验室可读）。"""
    library = await _get_visible_library(session, library_id, user)
    data = await graph_service.library_graph(session, library_id=library.id)
    return GraphResponse(**data)


@router.get("/libraries/{library_id}/notes", response_model=NotebookPage)
async def library_notebook(
    library_id: uuid.UUID,
    q: str | None = Query(default=None),
    paper_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> NotebookPage:
    """库笔记本：我在该库论文上写的笔记聚合（搜索 + 分页 + 按论文过滤）。"""
    library = await _get_visible_library(session, library_id, user)
    rows, total = await notes_service.list_library_notes(
        session,
        library_id=library.id,
        author_id=user.id,
        q=q,
        paper_id=paper_id,
        page=page,
        size=size,
    )
    items = [
        NoteWithPaper(
            id=note.id,
            paper_id=note.paper_id,
            author_id=note.author_id,
            author_name=author_name,
            content=note.content,
            created_at=note.created_at,
            updated_at=note.updated_at,
            paper_title=paper_title,
        )
        for note, author_name, paper_title in rows
    ]
    return NotebookPage(items=items, total=total, page=page, size=size)


@router.post("/libraries/{library_id}/chat")
async def chat_with_library(
    library_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """库文献对话：跨库内文献检索 + stage=reading 流式回答（全实验室可读，费用记个人）。

    事件：``sources``（引用来源清单）→ ``delta``* → ``done``；错误 ``error`` 后关流。
    """
    library = await _get_visible_library(session, library_id, user)
    user_id = user.id  # 先快照：检索失败路径的 rollback 会使 ORM 对象过期
    project_id = library.project_id
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()
    messages, sources = await library_chat_service.build_library_messages_for_library(
        session, library=library, question=data.question, history=history, llm=llm, user_id=user_id
    )

    async def event_stream() -> AsyncIterator[str]:
        yield _sse_frame("sources", {"items": [asdict(source) for source in sources]})
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for chunk in llm.stream(
                    "reading", messages, user_id=user_id, project_id=project_id
                ):
                    await queue.put(("delta", chunk))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成 error 事件后关流
                logger.warning("library chat stream failed", exc_info=True)
                await queue.put(("error", f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(pump())
        collected: list[str] = []
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "delta":
                    collected.append(payload or "")
                    yield _sse_frame("delta", {"text": payload})
                elif kind == "done":
                    usage = {
                        "prompt_tokens": sum(estimate_tokens(m.content) for m in messages),
                        "completion_tokens": estimate_tokens("".join(collected)),
                    }
                    yield _sse_frame("done", {"usage": usage})
                    return
                else:  # error
                    yield _sse_frame("error", {"detail": payload})
                    return
        finally:
            task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---- P6 治理：重复论文合并 ----


@router.get(
    "/libraries/{library_id}/duplicate-candidates",
    response_model=list[DuplicateCandidateGroup],
)
async def list_duplicate_candidates(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DuplicateCandidateGroup]:
    """库内疑似重复论文（可管理者）：arxiv/doi 同源不同行，或规范化标题相同。"""
    library = await _get_managed_library(session, library_id, user)
    groups = await paper_merge_service.duplicate_candidates(session, library_id=library.id)
    return [DuplicateCandidateGroup(**group) for group in groups]


@router.post("/papers/merge", response_model=PaperMergeResult)
async def merge_papers(
    data: PaperMergeRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperMergeResult:
    """合并重复论文（不可撤销）：drop 行的全部归属并入 keep 后删除 drop。

    权限：平台 admin，或 keep/drop 任一所在方向库的可管理者。
    """
    if user.role != "admin":
        libraries = (
            (
                await session.execute(
                    select(DirectionLibrary)
                    .join(LibraryPaper, LibraryPaper.library_id == DirectionLibrary.id)
                    .where(LibraryPaper.paper_id.in_([data.keep_id, data.drop_id]))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        allowed = False
        for library in libraries:
            if await libraries_service.can_manage_library(session, user=user, library=library):
                allowed = True
                break
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="PAPER_MERGE_FORBIDDEN")
    try:
        report = await paper_merge_service.merge_papers(
            session, keep_id=data.keep_id, drop_id=data.drop_id
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return PaperMergeResult(
        kept_id=data.keep_id,
        dropped_id=data.drop_id,
        dropped_dedup_key=report.pop("dropped_dedup_key"),
        details={k: v for k, v in report.items() if k not in ("kept_id", "dropped_id")},
    )
