"""Persistent administrator settings for literature discovery.

Provider credentials are encrypted at rest and are never returned by the read
endpoint.  A key list is replaced atomically when supplied, which makes key
rotation explicit and avoids accidentally deleting an existing pool.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.system_setting import SystemSetting

SETTING_KEY = "literature_search"
SUPPORTED_SOURCES = (
    "openalex",
    "semantic",
    "arxiv",
    "pubmed",
    "crossref",
    "europepmc",
    "hal",
    "core",
    "base",
    "sciverse",
)
DEFAULT_SCORE_WEIGHTS = {
    "relevance": 0.45,
    "quality": 0.2,
    "novelty": 0.2,
    "recency": 0.15,
}
DEFAULTS: dict[str, Any] = {
    "sources": ["openalex", "semantic", "arxiv", "pubmed", "crossref"],
    "requested_count": 20,
    "candidate_budget": 80,
    "start_year": None,
    "end_year": None,
    "score_weights": DEFAULT_SCORE_WEIGHTS,
    "provider_keys": {},
}


class InvalidLiteratureSettingError(ValueError):
    """A field failed administrator setting validation."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field


def _as_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise InvalidLiteratureSettingError(field, "must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLiteratureSettingError(field, "must be an integer") from exc
    if not minimum <= result <= maximum:
        raise InvalidLiteratureSettingError(field, f"must be between {minimum} and {maximum}")
    return result


def _normalize(data: Any) -> dict[str, Any]:
    source = data if isinstance(data, Mapping) else {}
    raw_sources = source.get("sources", DEFAULTS["sources"])
    if not isinstance(raw_sources, list):
        raise InvalidLiteratureSettingError("sources", "must be a list")
    sources = list(
        dict.fromkeys(str(item).strip().lower() for item in raw_sources if str(item).strip())
    )
    unknown = [item for item in sources if item not in SUPPORTED_SOURCES]
    if unknown:
        raise InvalidLiteratureSettingError("sources", f"unsupported source: {unknown[0]}")

    start_year = source.get("start_year")
    end_year = source.get("end_year")
    if start_year is not None:
        start_year = _as_int(start_year, "start_year", minimum=1800, maximum=3000)
    if end_year is not None:
        end_year = _as_int(end_year, "end_year", minimum=1800, maximum=3000)
    if start_year is not None and end_year is not None and start_year > end_year:
        raise InvalidLiteratureSettingError("year_window", "start_year must not exceed end_year")

    raw_weights = source.get("score_weights", DEFAULTS["score_weights"])
    if not isinstance(raw_weights, Mapping):
        raise InvalidLiteratureSettingError("score_weights", "must be an object")
    weights: dict[str, float] = {}
    for key, value in raw_weights.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidLiteratureSettingError(f"score_weights.{key}", "must be numeric") from exc
        if not math.isfinite(number) or number < 0:
            raise InvalidLiteratureSettingError(
                f"score_weights.{key}", "must be finite and non-negative"
            )
        weights[str(key)] = number
    if not weights or sum(weights.values()) <= 0:
        raise InvalidLiteratureSettingError(
            "score_weights", "at least one positive weight is required"
        )

    return {
        "sources": sources,
        "requested_count": _as_int(
            source.get("requested_count", 20), "requested_count", minimum=1, maximum=200
        ),
        "candidate_budget": _as_int(
            source.get("candidate_budget", 80), "candidate_budget", minimum=1, maximum=1000
        ),
        "start_year": start_year,
        "end_year": end_year,
        "score_weights": weights,
    }


def _mask(token: str) -> str:
    return f"••••{token[-4:]}" if len(token) >= 4 else "••••"


def _masked(value: Mapping[str, Any] | None) -> dict[str, Any]:
    keys = value.get("provider_keys") if isinstance(value, Mapping) else {}
    result: dict[str, list[dict[str, Any]]] = {}
    if isinstance(keys, Mapping):
        for source, pool in keys.items():
            if isinstance(pool, list):
                result[str(source)] = [
                    {
                        "index": index,
                        "configured": bool(item),
                        "preview": _mask(decrypt_secret(str(item))) if item else "••••",
                    }
                    for index, item in enumerate(pool)
                    if item
                ]
    return result


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    normalized = {**DEFAULTS, **_normalize(value)}
    normalized["provider_keys"] = _masked(value)
    normalized["provider_health"] = dict(value.get("provider_health") or {})
    return normalized


async def get_runtime_settings(session: AsyncSession) -> dict[str, Any]:
    """Return decrypted provider credentials for trusted server-side callers only."""
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    normalized = {**DEFAULTS, **_normalize(value)}
    encrypted = value.get("provider_keys") if isinstance(value, Mapping) else {}
    pools: dict[str, list[str]] = {}
    if isinstance(encrypted, Mapping):
        for source, items in encrypted.items():
            if isinstance(items, list):
                pools[str(source)] = [decrypt_secret(str(item)) for item in items if item]
    normalized["provider_keys"] = pools
    return normalized


async def update_settings(session: AsyncSession, data: Mapping[str, Any]) -> dict[str, Any]:
    row = await session.get(SystemSetting, SETTING_KEY)
    previous = row.value if row is not None and isinstance(row.value, Mapping) else {}
    normalized = _normalize({**(previous if isinstance(previous, Mapping) else {}), **dict(data)})
    if "provider_keys" in data and data["provider_keys"] is not None:
        raw_pools = data["provider_keys"]
        if not isinstance(raw_pools, Mapping):
            raise InvalidLiteratureSettingError("provider_keys", "must be an object")
        encrypted: dict[str, list[str]] = {}
        for source, values in raw_pools.items():
            source_name = str(source).strip().lower()
            if source_name not in SUPPORTED_SOURCES:
                raise InvalidLiteratureSettingError(
                    "provider_keys", f"unsupported source: {source_name}"
                )
            if not isinstance(values, list) or any(not str(item).strip() for item in values):
                raise InvalidLiteratureSettingError(
                    f"provider_keys.{source_name}", "must be a non-empty string list"
                )
            encrypted[source_name] = [encrypt_secret(str(item).strip()) for item in values]
        normalized["provider_keys"] = encrypted
    else:
        normalized["provider_keys"] = (
            dict(previous.get("provider_keys") or {})
            if isinstance(previous, Mapping)
            else {}
        )
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=normalized))
    else:
        row.value = normalized
    await session.commit()
    return await get_settings(session)


async def record_provider_health(
    session: AsyncSession, *, source: str, ok: bool, detail: str
) -> None:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, Mapping) else {}
    health = dict(value.get("provider_health") or {})
    health[source] = {"ok": ok, "detail": detail[:500], "checked_at": time.time()}
    value["provider_health"] = health
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
