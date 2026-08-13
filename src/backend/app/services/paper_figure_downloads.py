"""Short-lived, signed download links for paper figures.

The link is a bearer credential: callers can download the image without exposing the
user's long-lived JWT to another HTTP client.  The download endpoint still re-checks
that the issuing user can currently read the paper.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import get_settings

_SIGNING_CONTEXT = b"polaris:paper-figure-download:v1:"
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 24 * 60 * 60


class InvalidFigureDownloadToken(ValueError):
    """The supplied download token is malformed, forged, or expired."""


@dataclass(slots=True, frozen=True)
class FigureDownloadClaims:
    user_id: uuid.UUID
    paper_id: uuid.UUID
    index: int
    expires_at: int


@dataclass(slots=True, frozen=True)
class FigureDownloadLink:
    url: str
    expires_at: str


def _signature(body: str) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    digest = hmac.new(secret, _SIGNING_CONTEXT + body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_token(
    *,
    user_id: uuid.UUID,
    paper_id: uuid.UUID,
    index: int,
    ttl_seconds: int | None = None,
    now: int | None = None,
) -> tuple[str, FigureDownloadClaims]:
    """Create a compact HMAC token bound to one user, paper, figure, and expiry."""
    settings = get_settings()
    raw_ttl = settings.mcp_download_link_ttl_seconds if ttl_seconds is None else ttl_seconds
    ttl = max(_MIN_TTL_SECONDS, min(_MAX_TTL_SECONDS, int(raw_ttl)))
    expires_at = (int(time.time()) if now is None else now) + ttl
    claims = FigureDownloadClaims(user_id, paper_id, index, expires_at)
    body = f"{user_id.hex}.{paper_id.hex}.{index}.{expires_at}"
    return f"{body}.{_signature(body)}", claims


def verify_token(token: str, *, now: int | None = None) -> FigureDownloadClaims:
    """Verify a token and return its typed claims."""
    try:
        user_hex, paper_hex, raw_index, raw_expiry, supplied_signature = token.split(".")
        body = ".".join((user_hex, paper_hex, raw_index, raw_expiry))
        if not hmac.compare_digest(supplied_signature, _signature(body)):
            raise InvalidFigureDownloadToken("invalid signature")
        claims = FigureDownloadClaims(
            user_id=uuid.UUID(hex=user_hex),
            paper_id=uuid.UUID(hex=paper_hex),
            index=int(raw_index),
            expires_at=int(raw_expiry),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InvalidFigureDownloadToken):
            raise
        raise InvalidFigureDownloadToken("malformed token") from exc
    current_time = int(time.time()) if now is None else now
    if claims.index < 0 or claims.expires_at <= current_time:
        raise InvalidFigureDownloadToken("expired or invalid token")
    return claims


def create_download_link(
    *,
    user_id: uuid.UUID,
    paper_id: uuid.UUID,
    index: int,
    base_url: str | None,
) -> FigureDownloadLink:
    """Create an absolute link when a public/request base URL is available."""
    token, claims = create_token(user_id=user_id, paper_id=paper_id, index=index)
    path = f"/api/paper-figure-download/{token}"
    url = f"{base_url.rstrip('/')}{path}" if base_url else path
    expires_at = datetime.fromtimestamp(claims.expires_at, tz=UTC).isoformat()
    return FigureDownloadLink(url=url, expires_at=expires_at)
