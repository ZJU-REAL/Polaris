"""Research Wiki 附属路由：检索 / Obsidian 导出 / 引用导出 / 项目统计
（docs/api-m2.md §3、§5、§6；docs/api-lit.md §6）。"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.project import Project
from app.models.user import User
from app.schemas.ingest import ProjectStatsRead
from app.schemas.paper import PaperRead, ScoredConcept, ScoredPaper, SearchResponse
from app.services import citations as citations_service
from app.services import concepts as concepts_service
from app.services import papers as papers_service
from app.services import projects as projects_service
from app.services import stats as stats_service
from app.services.wiki_export import build_obsidian_zip

router = APIRouter(tags=["wiki"])


async def _member_project(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


@router.get("/projects/{project_id}/search", response_model=SearchResponse)
async def search(
    project_id: uuid.UUID,
    q: str = Query(min_length=1),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchResponse:
    await _member_project(session, project_id, user)

    mode_used = "keyword"
    reranked = False
    paper_rows: list = []
    if mode == "semantic" and papers_service.semantic_search_supported(session):
        try:
            vectors = await get_llm_router().embed([q], user_id=user.id, project_id=project_id)
            candidates = await papers_service.semantic_search_papers(
                session,
                project_id=project_id,
                query_vector=vectors[0],
                limit=max(papers_service.RERANK_CANDIDATES, limit),
            )
            mode_used = "semantic"
            # 向量召回 top 30 → rerank 取 top limit；失败降级为纯向量分
            paper_rows, reranked = await papers_service.rerank_paper_rows(
                get_llm_router(),
                query=q,
                rows=candidates,
                limit=limit,
                user_id=user.id,
                project_id=project_id,
            )
        except NotImplementedError:
            mode_used = "keyword"  # embedding 路由的 provider 不支持 → 回退
    if mode_used == "keyword":
        paper_rows = await papers_service.keyword_search_papers(
            session, project_id=project_id, q=q, limit=limit
        )
    concept_rows = await papers_service.keyword_search_concepts(
        session, project_id=project_id, q=q, limit=limit
    )

    concepts = []
    for concept, score in concept_rows:
        count = await concepts_service.paper_count_of(session, concept.id)
        concepts.append(
            ScoredConcept(
                id=concept.id,
                project_id=concept.project_id,
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


@router.get("/projects/{project_id}/export/obsidian")
async def export_obsidian(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    project = await _member_project(session, project_id, user)
    content = await build_obsidian_zip(session, project)
    filename = f"{project.slug}-wiki.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/export/citations")
async def export_citations(
    project_id: uuid.UUID,
    format_: str = Query(default="bibtex", alias="format", pattern="^(bibtex|csl-json)$"),
    status_filter: str | None = Query(default=None, alias="status"),
    tag: str | None = Query(default=None),
    starred: bool | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """引用导出：BibTeX / CSL-JSON（docs/api-lit.md §6）。

    过滤参数与论文列表一致；缺省导出 status in (compiled, included)。
    """
    project = await _member_project(session, project_id, user)
    papers = await citations_service.papers_for_export(
        session,
        project_id=project_id,
        user_id=user.id,
        status=status_filter,
        tag=tag,
        starred=starred,
    )
    if format_ == "bibtex":
        return Response(
            content=citations_service.build_bibtex(papers),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project.slug}-citations.bib"'},
        )
    return Response(
        content=json.dumps(citations_service.build_csl_json(papers), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{project.slug}-citations.json"'},
    )


@router.get("/projects/{project_id}/stats", response_model=ProjectStatsRead)
async def project_stats(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectStatsRead:
    await _member_project(session, project_id, user)
    data = await stats_service.project_stats(session, project_id)
    return ProjectStatsRead(**data)
