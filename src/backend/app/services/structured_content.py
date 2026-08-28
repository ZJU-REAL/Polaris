"""Authorized, path-safe access to immutable structured paper content."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import posixpath
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from app.core.config import get_settings
from app.models.paper_content import PaperContentVersion

_SIGNING_CONTEXT = b"polaris:structured-content:v1:"
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 60 * 60
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_TABLE_EXTENSIONS = {".csv", ".html", ".htm", ".xlsx"}
_MARKDOWN_DESTINATION = re.compile(
    r"(?P<prefix>!?\[[^\]\n]*\]\(\s*)(?P<reference><[^>\n]+>|[^)\s]+)(?P<suffix>[^)\n]*\))"
)
_HTML_DESTINATION = re.compile(
    r"(?P<prefix>\b(?:src|href)\s*=\s*[\"'])(?P<reference>[^\"']+)(?P<suffix>[\"'])",
    re.IGNORECASE,
)


class StructuredContentError(RuntimeError):
    """Persisted structured content is missing or violates its storage contract."""


class InvalidStructuredContentToken(ValueError):
    """A signed structured-content URL is malformed, forged, or expired."""


@dataclass(slots=True, frozen=True)
class StructuredContentClaims:
    user_id: uuid.UUID
    library_id: uuid.UUID
    paper_id: uuid.UUID
    version_id: uuid.UUID
    relative_path: str
    expires_at: int


@dataclass(slots=True, frozen=True)
class StructuredResource:
    kind: Literal["markdown", "text", "image", "table"]
    relative_path: str
    file_path: Path
    media_type: str
    byte_size: int
    sha256: str


@dataclass(slots=True, frozen=True)
class StructuredContentBundle:
    content_format: Literal["mineru_markdown", "plain_text", "unavailable"]
    content_hash: str | None
    markdown: StructuredResource | None
    text: StructuredResource | None
    assets: tuple[StructuredResource, ...]

    def resource(self, relative_path: str) -> StructuredResource | None:
        for item in (self.markdown, self.text, *self.assets):
            if item is not None and item.relative_path == relative_path:
                return item
        return None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signature(body: str) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    digest = hmac.new(secret, _SIGNING_CONTEXT + body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _safe_relative_path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise StructuredContentError("STRUCTURED_CONTENT_PATH_INVALID")
    return path.as_posix()


def create_token(
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    version_id: uuid.UUID,
    relative_path: str,
    expires_at: int | None = None,
    now: int | None = None,
) -> tuple[str, StructuredContentClaims]:
    current = int(time.time()) if now is None else int(now)
    ttl = max(
        _MIN_TTL_SECONDS,
        min(_MAX_TTL_SECONDS, int(get_settings().structured_content_link_ttl_seconds)),
    )
    expiry = current + ttl if expires_at is None else int(expires_at)
    path = _safe_relative_path(relative_path)
    claims = StructuredContentClaims(
        user_id=user_id,
        library_id=library_id,
        paper_id=paper_id,
        version_id=version_id,
        relative_path=path,
        expires_at=expiry,
    )
    payload = {
        "e": expiry,
        "l": library_id.hex,
        "p": paper_id.hex,
        "u": user_id.hex,
        "v": version_id.hex,
        "x": path,
    }
    body = _b64encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    )
    return f"{body}.{_signature(body)}", claims


def verify_token(token: str, *, now: int | None = None) -> StructuredContentClaims:
    try:
        body, supplied_signature = token.split(".", 1)
        if not hmac.compare_digest(supplied_signature, _signature(body)):
            raise InvalidStructuredContentToken("invalid signature")
        payload = json.loads(_b64decode(body).decode("ascii"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        claims = StructuredContentClaims(
            user_id=uuid.UUID(hex=str(payload["u"])),
            library_id=uuid.UUID(hex=str(payload["l"])),
            paper_id=uuid.UUID(hex=str(payload["p"])),
            version_id=uuid.UUID(hex=str(payload["v"])),
            relative_path=_safe_relative_path(payload["x"]),
            expires_at=int(payload["e"]),
        )
    except InvalidStructuredContentToken:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidStructuredContentToken("malformed token") from exc
    current = int(time.time()) if now is None else int(now)
    if claims.expires_at <= current:
        raise InvalidStructuredContentToken("expired token")
    return claims


def create_resource_url(
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    version_id: uuid.UUID,
    relative_path: str,
    expires_at: int | None = None,
) -> tuple[str, StructuredContentClaims]:
    token, claims = create_token(
        user_id=user_id,
        library_id=library_id,
        paper_id=paper_id,
        version_id=version_id,
        relative_path=relative_path,
        expires_at=expires_at,
    )
    return f"/api/structured-content-assets/{token}", claims


def _version_root(version: PaperContentVersion) -> Path:
    return (Path(get_settings().data_dir) / "paper-content" / str(version.id)).resolve()


def _path_from_key(version: PaperContentVersion, value: str | None) -> tuple[Path, str] | None:
    if not value:
        return None
    root = _version_root(version)
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise StructuredContentError("STRUCTURED_CONTENT_PATH_INVALID") from exc
    return resolved, _safe_relative_path(PurePosixPath(*relative.parts).as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resource(
    version: PaperContentVersion,
    value: str | None,
    *,
    kind: Literal["markdown", "text", "image", "table"],
) -> StructuredResource | None:
    resolved = _path_from_key(version, value)
    if resolved is None:
        return None
    path, relative = resolved
    if not path.is_file():
        raise StructuredContentError("STRUCTURED_CONTENT_FILE_MISSING")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind == "markdown":
        media_type = "text/markdown"
    elif kind == "text":
        media_type = "text/plain"
    return StructuredResource(
        kind=kind,
        relative_path=relative,
        file_path=path,
        media_type=media_type,
        byte_size=path.stat().st_size,
        sha256=_sha256(path),
    )


def _load_manifest(version: PaperContentVersion) -> dict:
    resolved = _path_from_key(version, version.manifest_key)
    if resolved is None:
        return {}
    path, _ = resolved
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StructuredContentError("STRUCTURED_MANIFEST_INVALID") from exc
    if not isinstance(payload, dict):
        raise StructuredContentError("STRUCTURED_MANIFEST_INVALID")
    return payload


def build_bundle(version: PaperContentVersion) -> StructuredContentBundle:
    """Resolve persisted files without exposing or trusting storage paths."""

    markdown = _resource(version, version.markdown_key, kind="markdown")
    text = _resource(version, version.text_key, kind="text")
    if markdown is None and text is None:
        return StructuredContentBundle("unavailable", None, None, None, ())

    assets: list[StructuredResource] = []
    if version.parser == "mineru":
        manifest = _load_manifest(version)
        seen: set[str] = set()
        for key, kind, extensions in (
            ("images", "image", _IMAGE_EXTENSIONS),
            ("tables", "table", _TABLE_EXTENSIONS),
        ):
            values = manifest.get(key) or []
            if not isinstance(values, list):
                raise StructuredContentError("STRUCTURED_MANIFEST_INVALID")
            for value in values:
                relative = _safe_relative_path(value)
                if relative in seen or PurePosixPath(relative).suffix.lower() not in extensions:
                    continue
                item = _resource(version, relative, kind=kind)
                if item is not None:
                    assets.append(item)
                    seen.add(relative)
        content_format: Literal["mineru_markdown", "plain_text", "unavailable"] = (
            "mineru_markdown" if markdown is not None else "plain_text"
        )
        content_hash = (markdown or text).sha256 if (markdown or text) is not None else None
        return StructuredContentBundle(content_format, content_hash, markdown, text, tuple(assets))

    return StructuredContentBundle(
        "plain_text",
        text.sha256 if text is not None else markdown.sha256 if markdown is not None else None,
        None,
        text or markdown,
        (),
    )


def _resolved_reference(markdown_path: str, value: str) -> str | None:
    reference = value[1:-1] if value.startswith("<") and value.endswith(">") else value
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/") or not parsed.path:
        return None
    decoded = unquote(parsed.path).replace("\\", "/")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(markdown_path), decoded))
    if joined == ".." or joined.startswith("../"):
        return None
    try:
        return _safe_relative_path(joined)
    except StructuredContentError:
        return None


def rewrite_markdown_asset_urls(
    markdown: str,
    *,
    markdown_path: str,
    asset_urls: dict[str, str],
) -> str:
    """Rewrite local Markdown and HTML asset references to signed URLs."""

    def markdown_replacement(match: re.Match[str]) -> str:
        resolved = _resolved_reference(markdown_path, match.group("reference"))
        url = asset_urls.get(resolved or "")
        if url is None:
            return match.group(0)
        return f'{match.group("prefix")}<{url}>{match.group("suffix")}'

    def html_replacement(match: re.Match[str]) -> str:
        resolved = _resolved_reference(markdown_path, match.group("reference"))
        url = asset_urls.get(resolved or "")
        if url is None:
            return match.group(0)
        return f'{match.group("prefix")}{url}{match.group("suffix")}'

    rewritten = _MARKDOWN_DESTINATION.sub(markdown_replacement, markdown)
    return _HTML_DESTINATION.sub(html_replacement, rewritten)
