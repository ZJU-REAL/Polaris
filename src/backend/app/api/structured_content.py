"""Authorized manifest and signed resources for parsed paper content."""

import hashlib
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.paper_content import PaperContentVersion
from app.models.user import User
from app.schemas.structured_content import (
    StructuredContentAssetRead,
    StructuredContentManifestRead,
)
from app.services import libraries as libraries_service
from app.services import paper_assets as asset_service
from app.services import paper_content as content_service
from app.services import papers as papers_service
from app.services import structured_content as content_access

router = APIRouter(tags=["structured-content"])


async def _visible_library_paper(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    user: User,
) -> None:
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")
    view = await papers_service.get_library_paper_view(
        session,
        library_id=library_id,
        project_id=None,
        paper_id=paper_id,
        with_concepts=False,
    )
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")


async def _authorized_version(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    user: User,
    version_id: uuid.UUID | None = None,
) -> PaperContentVersion:
    await _visible_library_paper(
        session,
        library_id=library_id,
        paper_id=paper_id,
        user=user,
    )
    if version_id is None:
        version = await content_service.current_content_version(session, paper_id=paper_id)
    else:
        version = await session.get(PaperContentVersion, version_id)
    if version is None or version.paper_id != paper_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")
    readable = await asset_service.readable_asset(
        session,
        asset_id=version.asset_id,
        library_id=library_id,
    )
    if readable is None or readable[0].paper_id != paper_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")
    return version


def _bundle(version: PaperContentVersion) -> content_access.StructuredContentBundle:
    try:
        return content_access.build_bundle(version)
    except content_access.StructuredContentError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="STRUCTURED_CONTENT_INVALID",
        ) from exc


def _resource_links(
    *,
    bundle: content_access.StructuredContentBundle,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    version: PaperContentVersion,
) -> tuple[dict[str, str], datetime | None]:
    resources = [
        item for item in (bundle.markdown, bundle.text, *bundle.assets) if item is not None
    ]
    if not resources:
        return {}, None
    links: dict[str, str] = {}
    expires_at: int | None = None
    for resource in resources:
        url, claims = content_access.create_resource_url(
            user_id=user_id,
            library_id=library_id,
            paper_id=version.paper_id,
            version_id=version.id,
            relative_path=resource.relative_path,
            expires_at=expires_at,
        )
        expires_at = claims.expires_at
        links[resource.relative_path] = url
    return links, datetime.fromtimestamp(expires_at, tz=UTC) if expires_at is not None else None


@router.get(
    "/libraries/{library_id}/papers/{paper_id}/structured-content",
    response_model=StructuredContentManifestRead,
)
async def get_structured_content_manifest(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> StructuredContentManifestRead:
    version = await _authorized_version(
        session,
        library_id=library_id,
        paper_id=paper_id,
        user=user,
    )
    bundle = _bundle(version)
    links, expires_at = _resource_links(
        bundle=bundle,
        user_id=user.id,
        library_id=library_id,
        version=version,
    )
    assets = [
        StructuredContentAssetRead(
            kind=item.kind,
            path=item.relative_path,
            media_type=item.media_type,
            byte_size=item.byte_size,
            sha256=item.sha256,
            url=links[item.relative_path],
            expires_at=expires_at,
        )
        for item in bundle.assets
        if expires_at is not None
    ]
    return StructuredContentManifestRead(
        content_version_id=version.id,
        paper_id=version.paper_id,
        asset_id=version.asset_id,
        version_no=version.version_no,
        parser=version.parser,
        parser_version=version.parser_version,
        parse_status=version.status,
        page_count=version.page_count,
        chunk_count=version.chunk_count,
        document_vector_state=version.document_vector_state,
        chunk_vector_state=version.chunk_vector_state,
        content_format=bundle.content_format,
        content_hash=bundle.content_hash,
        markdown_url=(
            links.get(bundle.markdown.relative_path) if bundle.markdown is not None else None
        ),
        text_url=links.get(bundle.text.relative_path) if bundle.text is not None else None,
        assets=assets,
        urls_expire_at=expires_at,
    )


def _cache_headers(
    *,
    sha256: str,
    expires_at: int,
    immutable: bool,
) -> dict[str, str]:
    max_age = max(0, expires_at - int(time.time()))
    directive = f"private, max-age={max_age}"
    if immutable:
        directive += ", immutable"
    return {
        "Cache-Control": directive,
        "ETag": f'"{sha256}"',
        "X-Content-SHA256": sha256,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
        ),
    }


@router.get("/structured-content-assets/{token}")
async def get_structured_content_resource(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        claims = content_access.verify_token(token)
    except content_access.InvalidStructuredContentToken as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="STRUCTURED_CONTENT_NOT_FOUND",
        ) from exc
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")
    version = await _authorized_version(
        session,
        library_id=claims.library_id,
        paper_id=claims.paper_id,
        user=user,
        version_id=claims.version_id,
    )
    bundle = _bundle(version)
    resource = bundle.resource(claims.relative_path)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="STRUCTURED_CONTENT_NOT_FOUND")

    if resource.kind == "markdown":
        try:
            markdown = resource.file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="STRUCTURED_CONTENT_INVALID",
            ) from exc
        asset_urls = {
            item.relative_path: content_access.create_resource_url(
                user_id=claims.user_id,
                library_id=claims.library_id,
                paper_id=claims.paper_id,
                version_id=claims.version_id,
                relative_path=item.relative_path,
                expires_at=claims.expires_at,
            )[0]
            for item in bundle.assets
        }
        rendered = content_access.rewrite_markdown_asset_urls(
            markdown,
            markdown_path=resource.relative_path,
            asset_urls=asset_urls,
        )
        content = rendered.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        return Response(
            content=content,
            media_type="text/markdown",
            headers=_cache_headers(
                sha256=digest,
                expires_at=claims.expires_at,
                immutable=False,
            ),
        )

    if resource.kind == "text":
        try:
            content = resource.file_path.read_text(encoding="utf-8").encode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="STRUCTURED_CONTENT_INVALID",
            ) from exc
        return Response(
            content=content,
            media_type="text/plain",
            headers=_cache_headers(
                sha256=resource.sha256,
                expires_at=claims.expires_at,
                immutable=True,
            ),
        )

    return FileResponse(
        resource.file_path,
        media_type=resource.media_type,
        filename=resource.file_path.name,
        content_disposition_type="inline",
        headers=_cache_headers(
            sha256=resource.sha256,
            expires_at=claims.expires_at,
            immutable=True,
        ),
    )
