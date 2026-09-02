"""Project-scoped interdisciplinary profile and dedicated literature library APIs."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.interdisciplinary import (
    InterdisciplinaryResearchProfile,
    InterdisciplinaryResearchProfileVersion,
)
from app.models.library_direction import DirectionLibrary, TopicSourceLibrary
from app.models.project import Project
from app.models.user import User
from app.schemas.interdisciplinary import (
    InterdisciplinaryConfirmation,
    InterdisciplinaryScopeDraft,
    InterdisciplinaryScopeRead,
    InterdisciplinaryScopeSuggestion,
    InterdisciplinaryScopeSuggestRequest,
)
from app.services import interdisciplinary_scope as scope_service
from app.services import libraries as libraries_service
from app.services import projects as projects_service
from app.services.interdisciplinary_retrieval import build_query_matrix, normalize_query_matrix
from app.services.interdisciplinary_workflows import (
    InterdisciplinaryScopeInvalidError,
    validate_disciplines,
)


async def _existing_dedicated_library(
    session: AsyncSession, project_id: uuid.UUID
) -> DirectionLibrary | None:
    """课题已有的跨学科库（唯一索引保证至多一条）。

    单独抽成函数，是为了让「两个并发请求都查到 None」这一幕能在测试里复现——
    否则 confirm 里的 IntegrityError 恢复路径只有真并发才走得到，等于没人守。
    """
    return await session.scalar(
        select(DirectionLibrary).where(
            DirectionLibrary.interdisciplinary_project_id == project_id,
            DirectionLibrary.library_kind == "interdisciplinary",
        )
    )

router = APIRouter(prefix="/projects/{project_id}/interdisciplinary", tags=["interdisciplinary"])
suggestion_router = APIRouter(tags=["interdisciplinary"])


@suggestion_router.post(
    "/projects/interdisciplinary-scope/suggest",
    response_model=InterdisciplinaryScopeSuggestion,
)
async def suggest_interdisciplinary_scope(
    data: InterdisciplinaryScopeSuggestRequest,
    user: User = Depends(current_active_user),
) -> InterdisciplinaryScopeSuggestion:
    return await scope_service.suggest_scope(data, user_id=user.id)


async def _managed_project(
    session: AsyncSession, project_id: uuid.UUID, user: User
) -> Project:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="PROJECT_FORBIDDEN")
    return project


async def _profile(
    session: AsyncSession, project_id: uuid.UUID
) -> InterdisciplinaryResearchProfile:
    profile = await session.scalar(
        select(InterdisciplinaryResearchProfile).where(
            InterdisciplinaryResearchProfile.project_id == project_id
        )
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="INTERDISCIPLINARY_SCOPE_NOT_FOUND")
    return profile


@router.get("/scope", response_model=InterdisciplinaryScopeRead)
async def get_interdisciplinary_scope(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InterdisciplinaryScopeRead:
    await _managed_project(session, project_id, user)
    return InterdisciplinaryScopeRead.model_validate(await _profile(session, project_id))


@router.get("/scope/versions", response_model=list[InterdisciplinaryScopeRead])
async def list_interdisciplinary_scope_versions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[InterdisciplinaryScopeRead]:
    await _managed_project(session, project_id, user)
    versions = list(
        (
            await session.execute(
                select(InterdisciplinaryResearchProfileVersion)
                .where(InterdisciplinaryResearchProfileVersion.project_id == project_id)
                .order_by(InterdisciplinaryResearchProfileVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [InterdisciplinaryScopeRead.model_validate(version) for version in versions]


@router.put("/scope", response_model=InterdisciplinaryScopeRead)
async def save_interdisciplinary_scope(
    project_id: uuid.UUID,
    data: InterdisciplinaryScopeDraft,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InterdisciplinaryScopeRead:
    project = await _managed_project(session, project_id, user)
    profile = await session.scalar(
        select(InterdisciplinaryResearchProfile)
        .where(InterdisciplinaryResearchProfile.project_id == project.id)
        .with_for_update()
    )
    if profile is None:
        profile = InterdisciplinaryResearchProfile(
            project_id=project.id,
            created_by=user.id,
            version=1,
            status="draft",
        )
        session.add(profile)
    else:
        profile.version += 1
        profile.status = "draft"
        profile.confirmed_by = None
        profile.confirmed_at = None
    profile.research_scope = data.research_scope.strip()
    profile.core_questions = [item.strip() for item in data.core_questions if item.strip()]
    profile.primary_domain = data.primary_domain.strip()
    profile.related_domains = [item.strip() for item in data.related_domains if item.strip()]
    profile.evidence_boundary = data.evidence_boundary.strip() if data.evidence_boundary else None
    profile.validation_conditions = data.validation_conditions
    profile.user_questions = data.user_questions
    profile.query_matrix = normalize_query_matrix(data.query_matrix or []) or build_query_matrix(
        topic=project.statement or project.name,
        primary_domain=profile.primary_domain,
        related_domains=profile.related_domains,
    )
    profile.evidence_balance = data.evidence_balance or {
        profile.primary_domain: 0.5,
        **{domain: 0.5 / len(profile.related_domains) for domain in profile.related_domains},
    }
    project.research_mode = "interdisciplinary"
    await session.flush()
    session.add(
        InterdisciplinaryResearchProfileVersion(
            profile_id=profile.id,
            project_id=profile.project_id,
            version=profile.version,
            status=profile.status,
            research_scope=profile.research_scope,
            core_questions=list(profile.core_questions),
            primary_domain=profile.primary_domain,
            related_domains=list(profile.related_domains),
            evidence_boundary=profile.evidence_boundary,
            validation_conditions=profile.validation_conditions,
            user_questions=profile.user_questions,
            query_matrix=profile.query_matrix,
            evidence_balance=profile.evidence_balance,
            created_by=user.id,
        )
    )
    await session.commit()
    await session.refresh(profile)
    return InterdisciplinaryScopeRead.model_validate(profile)


@router.post("/scope/confirm", response_model=InterdisciplinaryConfirmation)
async def confirm_interdisciplinary_scope(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InterdisciplinaryConfirmation:
    project = await _managed_project(session, project_id, user)
    profile = await _profile(session, project.id)
    if not profile.core_questions or not profile.related_domains:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="INTERDISCIPLINARY_SCOPE_INVALID"
        )
    try:
        validate_disciplines(profile.primary_domain, list(profile.related_domains))
    except InterdisciplinaryScopeInvalidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="INTERDISCIPLINARY_SCOPE_INVALID"
        ) from exc
    profile.status = "confirmed"
    profile.confirmed_by = user.id
    profile.confirmed_at = datetime.now(UTC)
    project.research_mode = "interdisciplinary"
    version = await session.scalar(
        select(InterdisciplinaryResearchProfileVersion).where(
            InterdisciplinaryResearchProfileVersion.profile_id == profile.id,
            InterdisciplinaryResearchProfileVersion.version == profile.version,
        )
    )
    if version is not None:
        version.status = "confirmed"
        version.confirmed_by = user.id
        version.confirmed_at = profile.confirmed_at

    library = await _existing_dedicated_library(session, project_id)
    if library is None:
        library = await libraries_service.create_library(
            session,
            name=f"{project.name} · Cross-disciplinary literature",
            statement=profile.research_scope,
            keywords={
                "include": [profile.primary_domain, *profile.related_domains],
                "interdisciplinary": True,
            },
            created_by=user.id,
        )
        library.library_kind = "interdisciplinary"
        library.interdisciplinary_project_id = project.id
        library.interdisciplinary_domains = [profile.primary_domain, *profile.related_domains]
        session.add(TopicSourceLibrary(topic_id=project.id, library_id=library.id))
    else:
        library.statement = profile.research_scope
        library.interdisciplinary_domains = [profile.primary_domain, *profile.related_domains]
    try:
        await session.commit()
    except IntegrityError as exc:
        # 唯一索引让并发确认幂等：另一个请求已经建好了库，这里回到那一个。
        # project_id 必须在 rollback 之前取好 —— rollback 会让 project 过期，
        # 之后再读 project.id 会在同步属性访问里触发异步 IO（MissingGreenlet），
        # 恢复路径自己就炸了，幂等兜底反而变成 500。
        await session.rollback()
        profile = await _profile(session, project_id)
        library = await _existing_dedicated_library(session, project_id)
        if library is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="INTERDISCIPLINARY_LIBRARY_CONFLICT",
            ) from exc
    await session.refresh(profile)
    return InterdisciplinaryConfirmation(
        profile=InterdisciplinaryScopeRead.model_validate(profile), library_id=library.id
    )
