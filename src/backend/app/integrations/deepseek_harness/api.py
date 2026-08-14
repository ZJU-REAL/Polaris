"""Versioned, read-only API consumed by the DeepSeek Harness plugin."""

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integration_auth import IntegrationPrincipal, require_skills_read
from app.core.db import get_session
from app.integrations.deepseek_harness import service
from app.integrations.deepseek_harness.schemas import (
    HarnessSkillCatalog,
    HarnessSkillDefinition,
)

router = APIRouter(
    prefix="/integrations/deepseek-harness/v1",
    tags=["deepseek-harness"],
)


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _cache_headers(revision: str) -> dict[str, str]:
    return {"ETag": _etag(revision), "Cache-Control": "private, no-cache"}


def _not_modified(if_none_match: str | None, revision: str) -> bool:
    if if_none_match is None:
        return False
    expected = _etag(revision)
    candidates = (candidate.strip() for candidate in if_none_match.split(","))
    return any(candidate in {"*", expected, f"W/{expected}"} for candidate in candidates)


@router.get("/skills", response_model=HarnessSkillCatalog)
async def list_harness_skills(
    response: Response,
    if_none_match: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    principal: IntegrationPrincipal = Depends(require_skills_read),
) -> HarnessSkillCatalog | Response:
    catalog = await service.skill_catalog(session, user_id=principal.user.id)
    headers = _cache_headers(catalog.revision)
    if _not_modified(if_none_match, catalog.revision):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return catalog


@router.get("/skills/{slug}", response_model=HarnessSkillDefinition)
async def get_harness_skill(
    slug: str,
    response: Response,
    if_none_match: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    principal: IntegrationPrincipal = Depends(require_skills_read),
) -> HarnessSkillDefinition | Response:
    definition = await service.skill_definition(session, user_id=principal.user.id, slug=slug)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_NOT_FOUND")
    headers = _cache_headers(definition.revision)
    if _not_modified(if_none_match, definition.revision):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return definition


@router.get("/skills/{slug}/files/{path:path}", response_class=PlainTextResponse)
async def get_harness_skill_file(
    slug: str,
    path: str,
    session: AsyncSession = Depends(get_session),
    principal: IntegrationPrincipal = Depends(require_skills_read),
) -> PlainTextResponse:
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_FILE_NOT_FOUND")
    found = await service.skill_file(session, user_id=principal.user.id, slug=slug, path=path)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_FILE_NOT_FOUND")
    content, revision = found
    return PlainTextResponse(
        content,
        headers={
            "ETag": _etag(revision),
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
