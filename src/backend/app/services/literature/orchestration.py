"""Shared checkpoint helpers for resumable provider searches."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def search_query_signature(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def load_provider_resume(
    checkpoint: dict[str, Any], *, provider_id: str, legacy_key: str | None = None
) -> dict[str, Any]:
    searches = checkpoint.get("search_resume")
    if isinstance(searches, dict) and isinstance(searches.get(provider_id), dict):
        return dict(searches[provider_id])
    if legacy_key and isinstance(checkpoint.get(legacy_key), dict):
        return dict(checkpoint[legacy_key])
    return {}


def store_provider_resume(
    checkpoint: dict[str, Any],
    *,
    provider_id: str,
    resume: dict[str, Any],
    legacy_key: str | None = None,
) -> None:
    searches = dict(checkpoint.get("search_resume") or {})
    searches[provider_id] = dict(resume)
    checkpoint["search_resume"] = searches
    if legacy_key:
        checkpoint[legacy_key] = dict(resume)
