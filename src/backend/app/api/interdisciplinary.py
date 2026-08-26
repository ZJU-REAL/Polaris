"""Project-scoped interdisciplinary profile and dedicated literature library APIs."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.interdisciplinary import InterdisciplinaryResearchProfile
from app.models.library_direction import DirectionLibrary, TopicSourceLibrary
from app.models.project import Project
from app.models.user import User
from app.schemas.interdisciplinary import (
    InterdisciplinaryConfirmation,
    InterdisciplinaryScopeDraft,
    InterdisciplinaryScopeRead,
)
from app.services import libraries as libraries_service
from app.services import projects as projects_service

router = APIRouter(prefix="/projects/{project_id}/interdisciplinary", tags=["interdisciplinary"])


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


@router.put("/scope", response_model=InterdisciplinaryScopeRead)
async def save_interdisciplinary_scope(
    project_id: uuid.UUID,
    data: InterdisciplinaryScopeDraft,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InterdisciplinaryScopeRead:
    project = await _managed_project(session, project_id, user)
    profile = await session.scalar(
        select(InterdisciplinaryResearchProfile).where(
            InterdisciplinaryResearchProfile.project_id == project.id
        )
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
    project.research_mode = "interdisciplinary"
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
    profile.status = "confirmed"
    profile.confirmed_by = user.id
    profile.confirmed_at = datetime.now(UTC)
    project.research_mode = "interdisciplinary"

    library = await session.scalar(
        select(DirectionLibrary).where(
            DirectionLibrary.interdisciplinary_project_id == project.id,
            DirectionLibrary.library_kind == "interdisciplinary",
        )
    )
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
        # The partial unique index makes confirmation idempotent under concurrent
        # requests. Re-read the winner instead of leaking a transient 500.
        await session.rollback()
        profile = await _profile(session, project.id)
        library = await session.scalar(
            select(DirectionLibrary).where(
                DirectionLibrary.interdisciplinary_project_id == project.id,
                DirectionLibrary.library_kind == "interdisciplinary",
            )
        )
        if library is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="INTERDISCIPLINARY_LIBRARY_CONFLICT",
            ) from exc
    await session.refresh(profile)
    return InterdisciplinaryConfirmation(
        profile=InterdisciplinaryScopeRead.model_validate(profile), library_id=library.id
    )
