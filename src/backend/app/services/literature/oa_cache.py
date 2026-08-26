"""OA PDF pre-cache and promotion primitives for literature discovery."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.literature_discovery import (
    LiteratureOaAttempt,
    LiteratureOaCache,
    LiteratureSearchHit,
)
from app.models.paper_assets import PdfBlob
from app.services.paper_assets import _validate_pdf, blob_storage_path

MAX_OA_BYTES = 150 * 1024 * 1024
MAX_REDIRECTS = 5


class OaDownloadError(RuntimeError):
    def __init__(self, code: str, detail: str, *, http_status: int | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


def _public_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


async def _assert_public_url(value: str) -> None:
    import asyncio

    if not _public_url(value):
        raise OaDownloadError("PDF_URL_PRIVATE", "OA URL is not public")
    host = urlsplit(value).hostname
    if host is None:
        raise OaDownloadError("PDF_URL_INVALID", "OA URL has no host")
    try:
        addresses = await asyncio.to_thread(
            lambda: {item[4][0] for item in socket.getaddrinfo(host, None)}
        )
    except socket.gaierror as exc:
        raise OaDownloadError("PDF_HOST_UNRESOLVED", str(exc)) from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise OaDownloadError("PDF_URL_PRIVATE", "OA URL resolves to a private network")


def candidate_urls(hit: LiteratureSearchHit) -> list[tuple[str, str]]:
    """Collect provider PDF links without turning metadata pages into PDFs."""
    metadata = hit.metadata_snapshot if isinstance(hit.metadata_snapshot, dict) else {}
    candidates: list[tuple[str, str]] = []

    def add(url: Any, source: str) -> None:
        value = str(url or "").strip()
        if value and _public_url(value) and value not in {item[0] for item in candidates}:
            candidates.append((value, source))

    add(hit.pdf_url, hit.source)
    for key, source in (
        ("pdf_url", hit.source),
        ("url_for_pdf", hit.source),
        ("openAccessPdf", "semantic"),
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            add(value.get("url_for_pdf") or value.get("pdf_url") or value.get("url"), source)
        else:
            add(value, source)
    for key, source in (("best_oa_location", "unpaywall"), ("primary_location", "openalex")):
        value = metadata.get(key)
        if isinstance(value, dict):
            add(value.get("url_for_pdf") or value.get("pdf_url"), source)
    for key, source in (
        ("oa_locations", "unpaywall"),
        ("locations", "openalex"),
        ("links", hit.source),
    ):
        for value in metadata.get(key) or []:
            if isinstance(value, dict):
                add(value.get("url_for_pdf") or value.get("pdf_url") or value.get("url"), source)
    if hit.arxiv_id:
        add(f"https://arxiv.org/pdf/{hit.arxiv_id}.pdf", "arxiv")
    if hit.pmid and str(hit.pmid).upper().startswith("PMC"):
        add(f"https://europepmc.org/articles/{hit.pmid}?pdf=render", "europepmc")
    return candidates


async def _download(url: str) -> tuple[bytes, str, int | None]:
    current = url
    timeout = httpx.Timeout(45.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _assert_public_url(current)
            response = await client.get(current, headers={"Accept": "application/pdf,*/*"})
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise OaDownloadError("PDF_REDIRECT_INVALID", "redirect has no location")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise OaDownloadError(
                    "PDF_HTTP_ERROR",
                    f"HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            content = response.content
            if len(content) > MAX_OA_BYTES:
                raise OaDownloadError("PDF_TOO_LARGE", "PDF exceeds the configured size limit")
            if not content.startswith(b"%PDF-"):
                raise OaDownloadError("PDF_SIGNATURE_INVALID", "response is not a PDF")
            return content, str(response.url), response.status_code
    raise OaDownloadError("PDF_REDIRECT_LIMIT", "too many PDF redirects")


async def cache_hit_pdf(
    session: AsyncSession, hit: LiteratureSearchHit
) -> LiteratureOaCache:
    cache = await session.scalar(
        select(LiteratureOaCache).where(LiteratureOaCache.hit_id == hit.id)
    )
    if cache is None:
        cache = LiteratureOaCache(hit_id=hit.id, status="pending")
        session.add(cache)
        await session.flush()
    if cache.status == "ready":
        return cache

    urls = candidate_urls(hit)
    if not urls:
        cache.status = "unavailable"
        cache.error_code = "OA_PDF_NOT_FOUND"
        cache.error_detail = "No verified OA PDF URL was returned by the providers"
        await session.flush()
        return cache

    cache.status = "downloading"
    cache.attempt_count = (cache.attempt_count or 0) + 1
    await session.flush()
    last_error: OaDownloadError | None = None
    for url, source in urls:
        attempt = LiteratureOaAttempt(cache_id=cache.id, url=url, status="running")
        session.add(attempt)
        await session.flush()
        try:
            content, final_url, http_status = await _download(url)
            await _validate_pdf_async(content)
            digest = hashlib.sha256(content).hexdigest()
            blob = await session.scalar(select(PdfBlob).where(PdfBlob.sha256 == digest))
            if blob is None:
                blob = PdfBlob(
                    sha256=digest,
                    byte_size=len(content),
                    storage_key=f"pdf-blobs/{digest[:2]}/{digest}.pdf",
                    content_type="application/pdf",
                    state="ready",
                )
                session.add(blob)
                await session.flush()
            path = blob_storage_path(digest)
            if not path.exists():
                await _write_blob(path, content)
            cache.status = "ready"
            cache.source_url = url
            cache.final_url = final_url
            cache.source = source
            cache.blob_id = blob.id
            cache.sha256 = digest
            cache.byte_size = len(content)
            cache.verification = {"pdf_signature": True, "parsed": True}
            cache.downloaded_at = datetime.now(UTC)
            cache.error_code = None
            cache.error_detail = None
            attempt.status = "success"
            attempt.http_status = http_status
            attempt.verification = cache.verification
            await session.flush()
            return cache
        except OaDownloadError as exc:
            last_error = exc
            attempt.status = "failed"
            attempt.http_status = exc.http_status
            attempt.error_code = exc.code
            attempt.error_detail = exc.detail
        except Exception as exc:  # noqa: BLE001
            last_error = OaDownloadError("PDF_PARSE_FAILED", str(exc))
            attempt.status = "failed"
            attempt.error_code = last_error.code
            attempt.error_detail = last_error.detail
    cache.status = "failed"
    cache.error_code = last_error.code if last_error else "OA_DOWNLOAD_FAILED"
    cache.error_detail = last_error.detail if last_error else "all OA URLs failed"
    await session.flush()
    return cache


async def _validate_pdf_async(content: bytes) -> None:
    import asyncio

    await asyncio.to_thread(_validate_pdf, content)


async def _write_blob(path: Path, content: bytes) -> None:
    import asyncio

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(content)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    await asyncio.to_thread(write)
