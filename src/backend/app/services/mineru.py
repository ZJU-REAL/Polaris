"""MinerU Cloud parser adapter used by the versioned PDF lifecycle."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings


class MineruCloudError(RuntimeError):
    """A MinerU upload, polling, or result conversion failure."""


StatusCallback = Callable[[str], Awaitable[None]]


def _tokens(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\s]+", raw or "") if item.strip()]


def _find(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _result_payload(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        for key in ("data", "result", "extract_result", "extractResult"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return nested
        return payload
    return None


def _markdown_result(markdown: str, *, pages: int = 0) -> dict[str, Any]:
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
    return {
        "parser": "mineru",
        "parser_version": "cloud",
        "markdown": text,
        "text": text,
        "pages": pages or current_page,
        "chunks": chunks,
        "manifest": {"pages": pages or current_page, "images": [], "tables": []},
    }


class MineruCloudParser:
    """Upload a PDF to MinerU Cloud, poll until completion, and normalize Markdown."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.mineru_timeout_seconds)
        self._owns_client = client is None
        self._tokens = _tokens(settings.mineru_api_tokens)
        self._token_index = 0
        self._semaphore = asyncio.Semaphore(settings.mineru_concurrency)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _next_token(self) -> str:
        if not self._tokens:
            raise MineruCloudError("MINERU_NOT_CONFIGURED")
        token = self._tokens[self._token_index % len(self._tokens)]
        self._token_index += 1
        return token

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
        raise MineruCloudError(f"MINERU_REQUEST_FAILED:{last}") from last

    async def parse(
        self, pdf_path: Path, *, on_status: StatusCallback | None = None
    ) -> dict[str, Any]:
        async with self._semaphore:
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
            upload_url = _find(upload, "url", "upload_url", "uploadUrl")
            if not upload_url:
                raise MineruCloudError("MINERU_UPLOAD_URL_MISSING")
            pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)
            response = await self._client.put(
                str(upload_url), content=pdf_bytes, headers={"Content-Type": "application/pdf"}
            )
            response.raise_for_status()
            if on_status:
                await on_status("mineru_parsing")

            deadline = asyncio.get_running_loop().time() + self._settings.mineru_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
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
                            md_response = await self._client.get(str(markdown_url))
                            md_response.raise_for_status()
                            return _markdown_result(
                                md_response.text,
                                pages=int(_find(row, "page_count", "pageCount") or 0),
                            )
                        zip_url = _find(row, "full_zip_url", "fullZipUrl", "zip_url", "zipUrl")
                        if zip_url:
                            archive = await self._client.get(str(zip_url))
                            archive.raise_for_status()
                            with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                                md_name = next(
                                    (
                                        name
                                        for name in bundle.namelist()
                                        if name.lower().endswith(".md")
                                    ),
                                    None,
                                )
                                if md_name:
                                    return _markdown_result(
                                        bundle.read(md_name).decode("utf-8", errors="replace")
                                    )
                        raise MineruCloudError("MINERU_RESULT_URL_MISSING")
                    if state in {"failed", "error", "failure"}:
                        raise MineruCloudError(
                            str(
                                _find(row, "err_msg", "error", "message")
                                or "MINERU_PARSE_FAILED"
                            )
                        )
                await asyncio.sleep(self._settings.mineru_poll_interval_seconds)
            raise MineruCloudError("MINERU_TIMEOUT")

    async def __call__(self, pdf_path: Path) -> dict[str, Any]:
        try:
            return await self.parse(pdf_path)
        finally:
            await self.aclose()
