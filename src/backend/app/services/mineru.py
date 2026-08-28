"""MinerU Cloud parser adapter used by the versioned PDF lifecycle."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

import httpx

from app.core.config import get_settings


class MineruCloudError(RuntimeError):
    """A MinerU upload, polling, or result conversion failure."""


def _safe_request_failure(prefix: str, error: Exception | None) -> MineruCloudError:
    """Build a stable error without persisting tokens or signed request URLs."""

    if isinstance(error, httpx.HTTPStatusError):
        detail = f"HTTP_{error.response.status_code}"
    elif isinstance(error, httpx.TimeoutException):
        detail = "TIMEOUT"
    elif isinstance(error, httpx.RequestError):
        detail = "NETWORK_ERROR"
    elif error is not None:
        detail = type(error).__name__.upper()
    else:
        detail = "UNKNOWN"
    return MineruCloudError(f"{prefix}:{detail}")


StatusCallback = Callable[[str], Awaitable[None]]
_SCHEDULERS_LOCK = Lock()
_MAX_ARCHIVE_FILES = 10_000
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class _MineruScheduler:
    """Share token rotation and concurrency limits across parser instances."""

    def __init__(self, tokens: list[str], concurrency: int) -> None:
        self.tokens = tuple(tokens)
        self.semaphore = asyncio.Semaphore(concurrency)
        self._index = 0
        self._lock = Lock()

    def next_token(self) -> str:
        if not self.tokens:
            raise MineruCloudError("MINERU_NOT_CONFIGURED")
        with self._lock:
            token = self.tokens[self._index % len(self.tokens)]
            self._index += 1
        return token


_SCHEDULERS: dict[tuple[tuple[str, ...], int], _MineruScheduler] = {}


def _scheduler(tokens: list[str], concurrency: int) -> _MineruScheduler:
    key = (tuple(tokens), concurrency)
    with _SCHEDULERS_LOCK:
        return _SCHEDULERS.setdefault(key, _MineruScheduler(tokens, concurrency))


def _tokens(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\s]+", raw or "") if item.strip()]


def _find(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _page_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _result_payload(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        for key in ("data", "result", "extract_result", "extractResult"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return nested
        return payload
    return None


def _safe_archive_name(name: str) -> str | None:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in path.parts[0]:
        return None
    return path.as_posix()


def _decode_markdown(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MineruCloudError("MINERU_MARKDOWN_ENCODING_INVALID") from exc


def _markdown_result(
    markdown: str,
    *,
    pages: int = 0,
    markdown_path: str = "content.md",
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = re.sub(r"\n{3,}", "\n\n", markdown.replace("\r\n", "\n")).strip()
    if not text:
        raise MineruCloudError("MINERU_MARKDOWN_EMPTY")
    chunks: list[dict[str, Any]] = []
    current_page = 1
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        page_match = re.search(r"(?:^|\n)\s*<!--\s*page\s*:?\s*(\d+)\s*-->", block, re.I)
        if page_match:
            current_page = int(page_match.group(1))
        chunks.append(
            {
                "text": block,
                "page_start": current_page,
                "page_end": current_page,
                "rects": [],
                "section_path": [],
                "anchor_meta": {"parser": "mineru"},
            }
        )
    artifacts = artifacts or []
    images = [item["path"] for item in artifacts if item["kind"] == "image"]
    tables = [item["path"] for item in artifacts if item["kind"] == "table"]
    return {
        "parser": "mineru",
        "parser_version": "cloud",
        "markdown": text,
        "text": text,
        "pages": pages or current_page,
        "chunks": chunks,
        "markdown_path": markdown_path,
        "artifacts": artifacts,
        "manifest": {"pages": pages or current_page, "images": images, "tables": tables},
    }


def _zip_result(content: bytes, *, pages: int = 0) -> dict[str, Any]:
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise MineruCloudError("MINERU_ARCHIVE_TOO_LARGE")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            infos = bundle.infolist()
            if len(infos) > _MAX_ARCHIVE_FILES:
                raise MineruCloudError("MINERU_ARCHIVE_TOO_MANY_FILES")
            files: list[tuple[str, bytes]] = []
            extracted_bytes = 0
            accepted_extensions = {
                ".md",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".svg",
                ".csv",
                ".html",
                ".htm",
                ".xlsx",
            }
            for info in infos:
                name = _safe_archive_name(info.filename)
                if (
                    name is None
                    or info.is_dir()
                    or PurePosixPath(name).suffix.lower() not in accepted_extensions
                ):
                    continue
                extracted_bytes += info.file_size
                if extracted_bytes > _MAX_ARCHIVE_BYTES:
                    raise MineruCloudError("MINERU_ARCHIVE_TOO_LARGE")
                files.append((name, bundle.read(info)))
    except MineruCloudError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise MineruCloudError("MINERU_ARCHIVE_INVALID") from exc
    markdown_files = [(name, data) for name, data in files if name.lower().endswith(".md")]
    if not markdown_files:
        raise MineruCloudError("MINERU_MARKDOWN_MISSING")
    markdown_name, markdown_bytes = max(markdown_files, key=lambda item: len(item[1]))
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    table_extensions = {".csv", ".html", ".htm", ".xlsx"}
    artifacts: list[dict[str, Any]] = []
    for name, data in files:
        suffix = PurePosixPath(name).suffix.lower()
        kind = (
            "image"
            if suffix in image_extensions
            else "table"
            if suffix in table_extensions
            else None
        )
        if kind is not None:
            artifacts.append({"path": name, "kind": kind, "content": data})
    return _markdown_result(
        _decode_markdown(markdown_bytes),
        pages=pages,
        markdown_path=markdown_name,
        artifacts=artifacts,
    )


class MineruCloudParser:
    """Upload a PDF to MinerU Cloud, poll until completion, and normalize Markdown."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.mineru_timeout_seconds)
        self._owns_client = client is None
        self._tokens = _tokens(settings.mineru_api_tokens)
        self._scheduler = _scheduler(self._tokens, settings.mineru_concurrency)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _next_token(self) -> str:
        return self._scheduler.next_token()

    async def _json(self, method: str, url: str, *, token: str, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self._settings.mineru_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt < self._settings.mineru_retries:
                    await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
        raise _safe_request_failure("MINERU_REQUEST_FAILED", last) from last

    async def _upload(self, url: str, content: bytes) -> None:
        last: Exception | None = None
        for attempt in range(self._settings.mineru_retries + 1):
            try:
                response = await self._client.put(
                    url, content=content, headers={"Content-Type": "application/pdf"}
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                last = exc
                if attempt < self._settings.mineru_retries:
                    await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
        raise _safe_request_failure("MINERU_UPLOAD_FAILED", last) from last

    async def _get_bytes(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(self._settings.mineru_retries + 1):
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                return response.content
            except httpx.HTTPError as exc:
                last = exc
                if attempt < self._settings.mineru_retries:
                    await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
        raise _safe_request_failure("MINERU_RESULT_DOWNLOAD_FAILED", last) from last

    async def parse(
        self, pdf_path: Path, *, on_status: StatusCallback | None = None
    ) -> dict[str, Any]:
        async with self._scheduler.semaphore:
            token = self._next_token()
            if on_status:
                await on_status("mineru_uploading")
            payload = await self._json(
                "POST",
                f"{self._settings.mineru_base_url.rstrip('/')}/file-urls/batch",
                token=token,
                json={"files": [{"name": pdf_path.name}]},
            )
            data = _result_payload(payload) or {}
            batch_id = _find(data, "batch_id", "batchId", "id")
            files = _find(data, "file_urls", "fileUrls", "files") or []
            if not batch_id or not isinstance(files, list) or not files:
                raise MineruCloudError("MINERU_UPLOAD_URL_MISSING")
            upload = files[0] if isinstance(files[0], Mapping) else {"url": files[0]}
            upload_url = _find(upload, "url", "file_url", "upload_url", "uploadUrl")
            if not upload_url:
                raise MineruCloudError("MINERU_UPLOAD_URL_MISSING")
            pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)
            await self._upload(str(upload_url), pdf_bytes)
            if on_status:
                await on_status("mineru_parsing")

            deadline = asyncio.get_running_loop().time() + self._settings.mineru_timeout_seconds
            polling_started = False
            while asyncio.get_running_loop().time() < deadline:
                if on_status and not polling_started:
                    await on_status("mineru_polling")
                    polling_started = True
                result = await self._json(
                    "GET",
                    f"{self._settings.mineru_base_url.rstrip('/')}/extract-results/batch/{batch_id}",
                    token=token,
                )
                parsed = _result_payload(result) or {}
                rows = _find(parsed, "extract_result_list", "extractResultList", "files") or []
                row = rows[0] if isinstance(rows, list) and rows else parsed
                if isinstance(row, Mapping):
                    state = str(_find(row, "state", "status") or "").lower()
                    if state in {"done", "success", "succeeded", "completed"}:
                        markdown_url = _find(row, "markdown_url", "markdownUrl", "md_url", "mdUrl")
                        if markdown_url:
                            if on_status:
                                await on_status("mineru_downloading_result")
                            markdown_bytes = await self._get_bytes(str(markdown_url))
                            return _markdown_result(
                                _decode_markdown(markdown_bytes),
                                pages=_page_count(_find(row, "page_count", "pageCount")),
                            )
                        zip_url = _find(row, "full_zip_url", "fullZipUrl", "zip_url", "zipUrl")
                        if zip_url:
                            if on_status:
                                await on_status("mineru_downloading_result")
                            archive_bytes = await self._get_bytes(str(zip_url))
                            return _zip_result(
                                archive_bytes,
                                pages=_page_count(_find(row, "page_count", "pageCount")),
                            )
                        raise MineruCloudError("MINERU_RESULT_URL_MISSING")
                    if state in {"failed", "error", "failure"}:
                        raise MineruCloudError(
                            str(_find(row, "err_msg", "error", "message") or "MINERU_PARSE_FAILED")
                        )
                await asyncio.sleep(self._settings.mineru_poll_interval_seconds)
            raise MineruCloudError("MINERU_TIMEOUT")

    async def __call__(self, pdf_path: Path) -> dict[str, Any]:
        try:
            return await self.parse(pdf_path)
        finally:
            await self.aclose()
