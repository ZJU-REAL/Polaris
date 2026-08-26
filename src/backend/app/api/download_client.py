"""User API-key driven protocol used by the Polaris browser extension."""

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.config import get_settings
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.download_client import DownloadApiKey, DownloadBatch, DownloadBatchItem
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper
from app.models.user import User
from app.schemas.download_client import (
    DownloadApiKeyCreated,
    DownloadArchiveMetadata,
    DownloadArchiveResult,
    DownloadBatchCreate,
    DownloadBatchCreated,
    DownloadBatchRead,
    DownloadCacheAck,
    DownloadHeartbeat,
    DownloadItemRead,
    DownloadResult,
)
from app.services import libraries as libraries_service
from app.services import paper_assets as asset_service
from app.services import paper_content as content_service

router = APIRouter(tags=["download-client"])

SCOPES = ["download_batch:read", "download_batch:update", "paper_pdf:upload"]
LEASE_MINUTES = 10
TERMINAL_ITEM_STATUSES = {"uploaded", "skipped", "failed", "blocked", "cancelled"}


def _hash_key(value: str) -> str:
    return hmac.new(
        get_settings().secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _expected_identity(paper: Paper) -> dict[str, str]:
    identity: dict[str, str] = {}
    if paper.dedup_key:
        identity["dedup_key"] = paper.dedup_key
    if paper.doi:
        identity["doi"] = paper.doi
    if paper.arxiv_id:
        identity["arxiv_id"] = paper.arxiv_id
    for key in ("pmid", "pmcid"):
        value = (paper.external_ids or {}).get(key)
        if value:
            identity[key] = str(value)
    return identity


def _normalize_doi(value: str) -> str:
    return value.strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def _identity_matches(metadata: DownloadArchiveMetadata, paper: Paper) -> bool:
    supplied = False
    if metadata.doi:
        supplied = True
        if not paper.doi or _normalize_doi(metadata.doi) != _normalize_doi(paper.doi):
            return False
    external = {str(k).lower(): str(v).lower() for k, v in (paper.external_ids or {}).items()}
    for name, value in (("pmid", metadata.pmid), ("pmcid", metadata.pmcid)):
        if value:
            supplied = True
            if external.get(name) != str(value).strip().lower():
                return False
    if metadata.arxiv_id:
        supplied = True
        if (
            not paper.arxiv_id
            or paper.arxiv_id.strip().lower() != metadata.arxiv_id.strip().lower()
        ):
            return False
    if supplied:
        return True
    return " ".join(metadata.title.casefold().split()) == " ".join(
        (paper.title or "").casefold().split()
    )


async def _require_managed_library(
    session: AsyncSession, *, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    library = await session.get(DirectionLibrary, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    if not await libraries_service.can_manage_library(session, user=user, library=library):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="DOWNLOAD_LIBRARY_FORBIDDEN")
    return library


async def download_client_user(
    x_polaris_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not x_polaris_api_key or not x_polaris_api_key.startswith("pol_dl_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="DOWNLOAD_API_KEY_REQUIRED")
    prefix = x_polaris_api_key[:24]
    row = (
        await session.execute(
            select(DownloadApiKey, User)
            .join(User, User.id == DownloadApiKey.user_id)
            .where(DownloadApiKey.key_prefix == prefix, DownloadApiKey.status == "active")
        )
    ).first()
    if row is None or not hmac.compare_digest(row[0].secret_hash, _hash_key(x_polaris_api_key)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="DOWNLOAD_API_KEY_INVALID")
    key, user = row
    now = datetime.now(UTC)
    if not user.is_active or (key.expires_at is not None and key.expires_at <= now):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="DOWNLOAD_API_KEY_EXPIRED")
    key.last_used_at = now
    await session.commit()
    return user


def _new_api_key() -> str:
    return "pol_dl_" + secrets.token_urlsafe(32)


@router.post("/me/download-api-key", response_model=DownloadApiKeyCreated)
async def rotate_download_api_key(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DownloadApiKeyCreated:
    existing = await session.scalar(select(DownloadApiKey).where(DownloadApiKey.user_id == user.id))
    raw = _new_api_key()
    if existing is None:
        key = DownloadApiKey(
            user_id=user.id,
            key_prefix=raw[:24],
            secret_hash=_hash_key(raw),
            scopes=SCOPES,
            status="active",
        )
        session.add(key)
    else:
        key = existing
        key.key_prefix = raw[:24]
        key.secret_hash = _hash_key(raw)
        key.scopes = SCOPES
        key.status = "active"
        key.revoked_at = None
        key.expires_at = None
    await session.commit()
    return DownloadApiKeyCreated(api_key=raw, key_prefix=key.key_prefix, created_at=key.created_at)


@router.delete("/me/download-api-key", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_download_api_key(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    key = await session.scalar(select(DownloadApiKey).where(DownloadApiKey.user_id == user.id))
    if key is not None:
        key.status = "revoked"
        key.revoked_at = datetime.now(UTC)
        await session.commit()


async def _cached_asset(
    session: AsyncSession, *, paper_id: uuid.UUID, library_id: uuid.UUID
) -> tuple[uuid.UUID, str] | None:
    rows = await asset_service.list_assets(session, paper_id=paper_id, library_id=library_id)
    for asset, blob, _grant in rows:
        if asset.state == "ready" and blob.state == "ready":
            return asset.id, blob.sha256
    return None


async def _reuse_public_asset(
    session: AsyncSession, *, paper_id: uuid.UUID, library: DirectionLibrary, user: User
) -> tuple[uuid.UUID, str] | None:
    try:
        grant = await asset_service.grant_public_asset_for_paper(
            session, paper_id=paper_id, target_library=library, user=user
        )
    except asset_service.AssetNotFoundError:
        return None
    asset = await session.get(asset_service.PaperAsset, grant.asset_id)
    blob = await session.get(asset_service.PdfBlob, asset.blob_id) if asset is not None else None
    if asset is None or blob is None:
        return None
    return asset.id, blob.sha256


def _public_result(result: dict | None) -> dict | None:
    if not result:
        return result
    return {key: value for key, value in result.items() if key != "_lease_token_hash"}


def _item_read(item: DownloadBatchItem, *, lease_token: str | None = None) -> DownloadItemRead:
    return DownloadItemRead(
        id=item.id,
        batch_id=item.batch_id,
        library_id=item.library_id,
        paper_id=item.paper_id,
        expected_identity=item.expected_identity,
        article_url=item.article_url,
        pdf_candidates=item.pdf_candidates,
        status=item.status,
        lease_until=item.lease_until,
        lease_token=lease_token,
        attempt_count=item.attempt_count,
        error=item.error,
        result=_public_result(item.result),
    )


async def _refresh_batch(session: AsyncSession, batch_id: uuid.UUID) -> None:
    batch = await session.get(DownloadBatch, batch_id)
    if batch is None:
        return
    statuses = list(
        (
            await session.execute(
                select(DownloadBatchItem.status).where(DownloadBatchItem.batch_id == batch_id)
            )
        ).scalars()
    )
    if not statuses:
        batch.status = "failed"
    elif all(value in TERMINAL_ITEM_STATUSES for value in statuses):
        uploaded = sum(value == "uploaded" for value in statuses)
        skipped = sum(value == "skipped" for value in statuses)
        if uploaded == len(statuses) or uploaded + skipped == len(statuses):
            batch.status = "completed"
        elif uploaded or skipped:
            batch.status = "partial"
        else:
            batch.status = "failed"
        batch.completed_at = datetime.now(UTC)
    elif any(value != "queued" for value in statuses):
        batch.status = "running"


@router.post("/download-batches", response_model=DownloadBatchCreated)
async def create_download_batch(
    data: DownloadBatchCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DownloadBatchCreated:
    batch = DownloadBatch(created_by=user.id, status="queued")
    session.add(batch)
    await session.flush()
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for target in data.targets:
        pair = (target.library_id, target.paper_id)
        if pair in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail="DUPLICATE_DOWNLOAD_TARGET"
            )
        seen.add(pair)
        library = await _require_managed_library(session, library_id=target.library_id, user=user)
        paper = await session.get(Paper, target.paper_id)
        if paper is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
        item = DownloadBatchItem(
            batch_id=batch.id,
            created_by=user.id,
            library_id=target.library_id,
            paper_id=paper.id,
            expected_identity=_expected_identity(paper),
            article_url=target.article_url or paper.url,
            pdf_candidates=target.pdf_candidates,
        )
        cached = await _cached_asset(session, paper_id=paper.id, library_id=target.library_id)
        if cached is None:
            cached = await _reuse_public_asset(
                session, paper_id=paper.id, library=library, user=user
            )
        if cached is not None:
            asset_id, sha256 = cached
            item.status = "skipped"
            item.result = {"reason": "already_cached", "asset_id": str(asset_id), "sha256": sha256}
        session.add(item)
    await _refresh_batch(session, batch.id)
    await session.commit()
    return DownloadBatchCreated(id=batch.id, status=batch.status, item_count=len(data.targets))


@router.get("/download-batches", response_model=list[DownloadBatchRead])
async def list_download_batches(
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DownloadBatchRead]:
    item_ids = select(DownloadBatchItem.batch_id).where(DownloadBatchItem.created_by == user.id)
    if library_id is not None:
        item_ids = item_ids.where(DownloadBatchItem.library_id == library_id)
    batches = list(
        (
            await session.execute(
                select(DownloadBatch)
                .where(DownloadBatch.created_by == user.id, DownloadBatch.id.in_(item_ids))
                .order_by(DownloadBatch.created_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    if not batches:
        return []
    items = list(
        (
            await session.execute(
                select(DownloadBatchItem)
                .where(DownloadBatchItem.batch_id.in_([batch.id for batch in batches]))
                .order_by(DownloadBatchItem.created_at)
            )
        ).scalars()
    )
    grouped: dict[uuid.UUID, list[DownloadItemRead]] = {}
    for item in items:
        grouped.setdefault(item.batch_id, []).append(_item_read(item))
    return [
        DownloadBatchRead(
            id=batch.id,
            status=batch.status,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            completed_at=batch.completed_at,
            items=grouped.get(batch.id, []),
        )
        for batch in batches
    ]


@router.get("/download-client/me")
async def get_download_client_me(user: User = Depends(download_client_user)) -> dict[str, str]:
    return {"user_id": str(user.id), "email": user.email}


@router.post("/download-client/items/claim", response_model=DownloadItemRead | None)
async def claim_download_item(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(download_client_user),
) -> DownloadItemRead | None:
    now = datetime.now(UTC)
    stmt = (
        select(DownloadBatchItem)
        .where(
            DownloadBatchItem.created_by == user.id,
            DownloadBatchItem.status.in_(("queued", "claimed", "downloading")),
            (DownloadBatchItem.lease_until.is_(None)) | (DownloadBatchItem.lease_until < now),
        )
        .order_by(DownloadBatchItem.created_at)
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None:
        return None
    item.status = "claimed"
    item.claimed_at = now
    item.heartbeat_at = now
    item.lease_until = now + timedelta(minutes=LEASE_MINUTES)
    item.attempt_count += 1
    lease_token = secrets.token_urlsafe(32)
    item.result = {"_lease_token_hash": hashlib.sha256(lease_token.encode()).hexdigest()}
    await _refresh_batch(session, item.batch_id)
    await session.commit()
    return _item_read(item, lease_token=lease_token)


async def _owned_item(session: AsyncSession, item_id: uuid.UUID, user: User) -> DownloadBatchItem:
    item = await session.get(DownloadBatchItem, item_id)
    if item is None or item.created_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="DOWNLOAD_ITEM_NOT_FOUND")
    return item


def _require_active_lease(item: DownloadBatchItem, token: str | None, *, now: datetime) -> None:
    expected = (item.result or {}).get("_lease_token_hash")
    actual = hashlib.sha256(token.encode()).hexdigest() if token else ""
    lease_until = item.lease_until
    if lease_until is not None and lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    if (
        item.status not in {"claimed", "downloading", "cached", "uploading"}
        or lease_until is None
        or lease_until <= now
        or not isinstance(expected, str)
        or not hmac.compare_digest(expected, actual)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="DOWNLOAD_ITEM_LEASE_INVALID")


@router.post("/download-client/items/{item_id}/heartbeat")
async def heartbeat_download_item(
    item_id: uuid.UUID,
    data: DownloadHeartbeat,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(download_client_user),
    x_polaris_lease_token: str | None = Header(default=None),
) -> dict[str, str]:
    item = await _owned_item(session, item_id, user)
    _require_active_lease(item, x_polaris_lease_token, now=datetime.now(UTC))
    item.status = data.status
    item.heartbeat_at = datetime.now(UTC)
    item.lease_until = datetime.now(UTC) + timedelta(minutes=LEASE_MINUTES)
    await session.commit()
    return {"status": item.status}


@router.post("/download-client/items/{item_id}/cache")
async def acknowledge_download_cache(
    item_id: uuid.UUID,
    data: DownloadCacheAck,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(download_client_user),
    x_polaris_lease_token: str | None = Header(default=None),
) -> dict[str, str | int]:
    item = await _owned_item(session, item_id, user)
    _require_active_lease(item, x_polaris_lease_token, now=datetime.now(UTC))
    item.status = "cached"
    item.heartbeat_at = datetime.now(UTC)
    item.lease_until = datetime.now(UTC) + timedelta(minutes=LEASE_MINUTES)
    item.result = {
        **(item.result or {}),
        "local_sha256": data.sha256.lower(),
        "local_byte_size": data.byte_size,
    }
    await session.commit()
    return {"status": item.status, "sha256": data.sha256.lower(), "byte_size": data.byte_size}


@router.post("/download-client/items/{item_id}/result")
async def report_download_result(
    item_id: uuid.UUID,
    data: DownloadResult,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(download_client_user),
    x_polaris_lease_token: str | None = Header(default=None),
) -> dict[str, str]:
    item = await _owned_item(session, item_id, user)
    if item.status in TERMINAL_ITEM_STATUSES:
        if item.status == data.status:
            return {"status": item.status}
        raise HTTPException(status.HTTP_409_CONFLICT, detail="DOWNLOAD_ITEM_TERMINAL")
    _require_active_lease(item, x_polaris_lease_token, now=datetime.now(UTC))
    item.status, item.error, item.result = data.status, data.error, data.evidence
    item.lease_until = None
    await _refresh_batch(session, item.batch_id)
    await session.commit()
    return {"status": item.status}


async def _archive_bound_pdf(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    paper: Paper,
    user: User,
    content: bytes,
    source_url: str | None,
    queue: TaskQueue,
    expected_sha256: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    if expected_sha256 and not hmac.compare_digest(
        expected_sha256.strip().lower(), hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("PDF_CHECKSUM_MISMATCH")
    membership, _ = await libraries_service.ensure_membership(
        session, library_id=library.id, paper_id=paper.id, status="included"
    )
    membership.status = "included"
    # Extension PDFs may be institution-authorized. Keep them inside the target
    # library; only explicitly public/OA assets may be reused across libraries.
    sharing_scope = "library"
    asset = await asset_service.create_or_reuse_asset(
        session,
        paper=paper,
        library=library,
        content=content,
        user=user,
        source="extension",
        source_locator=source_url,
        identity_key=paper.dedup_key,
        identity_status="verified",
        sharing_scope=sharing_scope,
    )
    current = await content_service.current_content_version(session, paper_id=paper.id)
    enqueue_parse = False
    if current is not None and current.asset_id == asset.id and current.status != "failed":
        version = current
    else:
        version = await content_service.create_content_version(
            session, asset=asset, parser="mineru"
        )
        enqueue_parse = True
    await session.commit()
    if enqueue_parse:
        await queue.enqueue(
            "parse_paper_content_task", str(version.id), str(user.id), str(library.id)
        )
    return asset.id, version.id, version.status


async def _complete_matching_items(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    asset_id: uuid.UUID,
    content_version_id: uuid.UUID,
) -> None:
    items = list(
        (
            await session.execute(
                select(DownloadBatchItem).where(
                    DownloadBatchItem.created_by == user_id,
                    DownloadBatchItem.library_id == library_id,
                    DownloadBatchItem.paper_id == paper_id,
                    DownloadBatchItem.status.notin_(TERMINAL_ITEM_STATUSES),
                )
            )
        ).scalars()
    )
    batch_ids: set[uuid.UUID] = set()
    for item in items:
        item.status = "uploaded"
        item.error = None
        item.lease_until = None
        item.result = {
            "asset_id": str(asset_id),
            "content_version_id": str(content_version_id),
        }
        batch_ids.add(item.batch_id)
    await session.flush()
    for batch_id in batch_ids:
        await _refresh_batch(session, batch_id)


@router.post("/download-client/items/{item_id}/pdf", status_code=status.HTTP_201_CREATED)
async def upload_downloaded_pdf(
    item_id: uuid.UUID,
    pdf: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    queue: TaskQueue = Depends(get_task_queue),
    user: User = Depends(download_client_user),
    x_polaris_lease_token: str | None = Header(default=None),
    x_polaris_pdf_sha256: str | None = Header(default=None),
) -> dict[str, str]:
    item = await _owned_item(session, item_id, user)
    if item.status in {"uploaded", "skipped"} and item.result:
        return {
            key: str(item.result[key])
            for key in ("asset_id", "content_version_id")
            if key in item.result
        }
    _require_active_lease(item, x_polaris_lease_token, now=datetime.now(UTC))
    library = await _require_managed_library(session, library_id=item.library_id, user=user)
    paper = await session.get(Paper, item.paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    content = await pdf.read(asset_service.MAX_PDF_BYTES + 1)
    expected_sha256 = (item.result or {}).get("local_sha256") or x_polaris_pdf_sha256
    try:
        asset_id, version_id, version_status = await _archive_bound_pdf(
            session,
            library=library,
            paper=paper,
            user=user,
            content=content,
            source_url=item.article_url,
            queue=queue,
            expected_sha256=expected_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - item failure is persisted for retry visibility
        await session.rollback()
        item = await session.get(DownloadBatchItem, item_id)
        if item is not None:
            item.status, item.error, item.lease_until = "failed", str(exc)[:4000], None
            await _refresh_batch(session, item.batch_id)
            await session.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    item.status = "uploaded"
    item.result = {"asset_id": str(asset_id), "content_version_id": str(version_id)}
    item.lease_until = None
    await _refresh_batch(session, item.batch_id)
    await session.commit()
    return {
        "asset_id": str(asset_id),
        "content_version_id": str(version_id),
        "status": version_status,
    }


@router.post(
    "/download-client/archive",
    response_model=DownloadArchiveResult,
    status_code=status.HTTP_201_CREATED,
)
async def archive_downloaded_pdf(
    metadata: str = Form(...),
    pdf: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    queue: TaskQueue = Depends(get_task_queue),
    user: User = Depends(download_client_user),
    x_polaris_pdf_sha256: str | None = Header(default=None),
) -> DownloadArchiveResult:
    try:
        archive = DownloadArchiveMetadata.model_validate(json.loads(metadata))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="DOWNLOAD_ARCHIVE_METADATA_INVALID"
        ) from exc
    library = await _require_managed_library(session, library_id=archive.library_id, user=user)
    paper = await session.get(Paper, archive.paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    if not _identity_matches(archive, paper):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="DOWNLOAD_ARCHIVE_IDENTITY_MISMATCH"
        )
    content = await pdf.read(asset_service.MAX_PDF_BYTES + 1)
    try:
        asset_id, version_id, version_status = await _archive_bound_pdf(
            session,
            library=library,
            paper=paper,
            user=user,
            content=content,
            source_url=archive.source_url,
            queue=queue,
            expected_sha256=x_polaris_pdf_sha256,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await _complete_matching_items(
        session,
        user_id=user.id,
        library_id=library.id,
        paper_id=paper.id,
        asset_id=asset_id,
        content_version_id=version_id,
    )
    await session.commit()
    return DownloadArchiveResult(
        asset_id=asset_id, content_version_id=version_id, status=version_status
    )
