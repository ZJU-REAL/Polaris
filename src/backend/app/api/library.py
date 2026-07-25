"""个人文献库路由（issue #108）：/me/library，用户级、方向无关。"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.core.redis import get_redis_dep
from app.models.paper import Paper
from app.models.user import User
from app.schemas.library import (
    LibraryEntryDetail,
    LibraryEntryRead,
    LibraryImportRead,
    LibraryNoteUpdate,
    LibraryPage,
    LibrarySaveRequest,
    LibraryStateRead,
    LibraryVisitCreate,
)
from app.schemas.paper import PaperManualCreate
from app.services import citations as citations_service
from app.services import paper_enrich as paper_enrich_service
from app.services import paper_import as paper_import_service
from app.services import papers as papers_service
from app.services import user_library as library_service

router = APIRouter(tags=["library"])


async def _get_member_paper(session: AsyncSession, paper_id: uuid.UUID, user: User) -> Paper:
    """校验论文可见性（所在方向的成员），防止越权拷快照。"""
    paper = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user.id, include_pool=True
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return paper


async def _get_own_entry(session: AsyncSession, entry_id: uuid.UUID, user: User):
    entry = await library_service.get_entry(session, user_id=user.id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ENTRY_NOT_FOUND")
    return entry


@router.get("/me/library", response_model=LibraryPage)
async def list_library(
    tab: str = Query(default="history", pattern="^(saved|history|trash)$"),
    q: str | None = Query(default=None),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    sort: str = Query(default="recent", pattern="^(recent|title|visits|year)$"),
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    author: str | None = Query(default=None),
    # 机构：快照里没有，按条目软引用的活体论文匹配（源论文已删的条目匹配不到）
    affiliation: str | None = Query(default=None),
    venue: str | None = Query(default=None),
    # 星标 / 阅读状态 / 我的标签：同机构，按条目软引用的活体论文匹配
    # （源论文已删的条目一律筛不出来，含 reading_status=unread）
    reading_status: str | None = Query(default=None, pattern="^(unread|reading|read)$"),
    starred: bool | None = Query(default=None),
    my_tag: str | None = Query(default=None, description="按我的标签过滤（只有本人可见）"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryPage:
    # 语义检索：仅「我的收藏」tab 有意义（浏览记录不做语义），候选=本人收藏且有向量的论文。
    # embed/rerank 记个人账（无课题上下文）；非 postgres / provider 不支持 → 回退关键词。
    if mode == "semantic" and tab == "saved" and q and papers_service.semantic_search_supported(
        session
    ):
        try:
            vectors = await get_llm_router().embed([q], user_id=user.id)
            candidates = await library_service.semantic_saved_entries(
                session,
                user_id=user.id,
                query_vector=vectors[0],
                limit=max(papers_service.RERANK_CANDIDATES, size),
            )
            ranked, _reranked = await papers_service.rerank_paper_rows(
                get_llm_router(), query=q, rows=candidates, limit=size, user_id=user.id
            )
            entries = [entry for entry, _ in ranked]
            return LibraryPage(
                items=[LibraryEntryRead.model_validate(e) for e in entries],
                total=len(entries),
                page=1,
                size=size,
                mode_used="semantic",
            )
        except NotImplementedError:
            pass  # embedding 路由的 provider 不支持 → 回退关键词
    items, total = await library_service.list_entries(
        session,
        user_id=user.id,
        tab=tab,
        q=q,
        sort=sort,
        page=page,
        size=size,
        year_from=year_from,
        year_to=year_to,
        author=author,
        affiliation=affiliation,
        venue=venue,
        reading_status=reading_status,
        starred=starred,
        my_tag=my_tag,
    )
    return LibraryPage(
        items=[LibraryEntryRead.model_validate(e) for e in items],
        total=total,
        page=page,
        size=size,
        mode_used="keyword",
    )


# 注意：/visits、/state、/trash/empty 必须注册在 /{entry_id} 之前，避免被路径参数抢占


@router.post(
    "/me/library/visits", response_model=LibraryEntryRead, status_code=status.HTTP_201_CREATED
)
async def record_visit(
    body: LibraryVisitCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    """阅读页打开时上报一次浏览（跨方向按 arxiv/doi/标题合并到同一条目）。"""
    paper = await _get_member_paper(session, body.paper_id, user)
    entry = await library_service.record_visit(session, user_id=user.id, paper=paper)
    return LibraryEntryRead.model_validate(entry)


@router.delete("/me/library/visits", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """清空浏览记录：未收藏条目删除，已收藏条目保留但清零访问统计。"""
    await library_service.clear_history(session, user_id=user.id)


@router.post("/me/library/trash/empty")
async def empty_library_trash(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, int]:
    """清空回收站：彻底删除本人全部已删除的条目，返回 {deleted}。"""
    deleted = await library_service.purge_trash(session, user_id=user.id)
    return {"deleted": deleted}


@router.get("/me/library/state", response_model=LibraryStateRead)
async def library_state(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryStateRead:
    paper = await _get_member_paper(session, paper_id, user)
    entry = await library_service.entry_for_paper(session, user_id=user.id, paper=paper)
    return LibraryStateRead(entry_id=entry.id if entry else None, saved=bool(entry and entry.saved))


@router.get("/me/library/export/citations")
async def export_personal_citations(
    format_: str = Query(default="bibtex", alias="format", pattern="^(bibtex|csl-json)$"),
    ids: str | None = Query(default=None, description="逗号分隔的论文 id（多选导出）"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """个人库引用导出：BibTeX / CSL-JSON。

    不传 ids 导出全部收藏；ids 指定时按 id 精确导出（多选导出），只含属于本人收藏的论文，
    非本人 / 非收藏 / 源方向已删除（无软引用）的一律不含。
    """
    paper_ids: list[uuid.UUID] | None = None
    if ids:
        try:
            paper_ids = [uuid.UUID(x) for x in ids.split(",") if x.strip()]
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="INVALID_IDS") from e
    papers = await citations_service.papers_for_personal_export(
        session, user_id=user.id, paper_ids=paper_ids
    )
    if format_ == "bibtex":
        return Response(
            content=citations_service.build_bibtex(papers),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="polaris-my-library-citations.bib"'
            },
        )
    return Response(
        content=json.dumps(citations_service.build_csl_json(papers), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="polaris-my-library-citations.json"'
        },
    )


@router.post("/me/library", response_model=LibraryEntryRead, status_code=status.HTTP_201_CREATED)
async def save_entry(
    body: LibrarySaveRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    """收藏：paper_id（从论文页收藏）或 entry_id（收藏一条浏览记录）。"""
    if body.paper_id is not None:
        paper = await _get_member_paper(session, body.paper_id, user)
        entry = await library_service.save_paper(session, user_id=user.id, paper=paper)
    else:
        entry = await _get_own_entry(session, body.entry_id, user)  # type: ignore[arg-type]
        entry = await library_service.set_saved(session, entry=entry, saved=True)
    return LibraryEntryRead.model_validate(entry)


@router.post(
    "/me/library/import", response_model=LibraryImportRead, status_code=status.HTTP_201_CREATED
)
async def import_entry(
    body: PaperManualCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> LibraryImportRead:
    """手动添加一篇文献到「我的收藏」：arXiv 编号 / DOI / BibTeX 三选一。

    平台已有这篇时直接复用（不重复解析），没有才抓取解析入池（不进任何方向库）；同一篇
    论文在本人回收站里时走「复活」而不是插新行（每人每篇只有一条）。新建或未处理完整时
    启动后台补全任务（下载/抽取/向量化，无库不打分），回传 task_id 供前端显示进度。
    """
    try:
        result = await paper_import_service.resolve_or_create_pool_paper(
            session, arxiv_id=body.arxiv_id, doi=body.doi, bibtex=body.bibtex
        )
    except paper_import_service.ParseFailedError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"PARSE_FAILED: {e}"
        ) from e
    # 收藏走个人库的既有路径：已有条目（含回收站里的）复活并置 saved，不会建第二行
    entry = await library_service.save_paper(session, user_id=user.id, paper=result.paper)
    task_id: str | None = None
    if result.created or not paper_enrich_service.paper_processing_complete(result.paper):
        task_id = await paper_enrich_service.launch_paper_enrichment(
            redis=redis,
            paper_id=result.paper.id,
            user_id=user.id,
            library_id=None,
            project_id=None,
        )
    return LibraryImportRead.model_validate(entry).model_copy(update={"task_id": task_id})


@router.get("/me/library/{entry_id}", response_model=LibraryEntryDetail)
async def get_entry_detail(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryDetail:
    """单条详情（含 wiki 快照）：源论文删除后前端右栏回退用。"""
    entry = await _get_own_entry(session, entry_id, user)
    return LibraryEntryDetail.model_validate(entry)


@router.post("/me/library/{entry_id}/restore", response_model=LibraryEntryRead)
async def restore_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    """从回收站召回：出回收站并回到收藏（与「收藏一条已有条目」同一条路径）。"""
    entry = await _get_own_entry(session, entry_id, user)
    entry = await library_service.set_saved(session, entry=entry, saved=True)
    return LibraryEntryRead.model_validate(entry)


@router.patch("/me/library/{entry_id}", response_model=LibraryEntryRead)
async def update_entry(
    entry_id: uuid.UUID,
    body: LibraryNoteUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    entry = await _get_own_entry(session, entry_id, user)
    entry.note = body.note
    await session.commit()
    await session.refresh(entry)
    return LibraryEntryRead.model_validate(entry)


@router.delete("/me/library/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_entry(
    entry_id: uuid.UUID,
    mode: str = Query(default="unsave", pattern="^(unsave|purge)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """unsave=取消收藏（条目移入回收站，可召回）；purge=彻底删除该条目。"""
    entry = await _get_own_entry(session, entry_id, user)
    if mode == "purge":
        await library_service.purge_entry(session, entry=entry)
    else:
        await library_service.set_saved(session, entry=entry, saved=False)
