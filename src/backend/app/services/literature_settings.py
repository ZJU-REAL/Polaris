"""Persistent administrator settings for literature discovery.

Provider credentials are encrypted at rest and are never returned by the read
endpoint.  A key list is replaced atomically when supplied, which makes key
rotation explicit and avoids accidentally deleting an existing pool.
"""

from __future__ import annotations

import math
import time
import uuid
import zlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.system_setting import SystemSetting

SETTING_KEY = "literature_search"
_SETTING_LOCK_ID = zlib.crc32(SETTING_KEY.encode("utf-8"))
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
    "evidence_quality": 0.20,
    "impact": 0.15,
    "novelty": 0.10,
    "recency": 0.10,
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


async def _setting_for_update(session: AsyncSession) -> SystemSetting | None:
    """Serialize read-modify-write updates to the shared settings document."""

    if session.get_bind().dialect.name == "postgresql":
        # A row lock cannot serialize the first write while the singleton row is absent.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _SETTING_LOCK_ID}
        )
    return await session.scalar(
        select(SystemSetting).where(SystemSetting.key == SETTING_KEY).with_for_update()
    )


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
    weights = (
        dict(DEFAULT_SCORE_WEIGHTS)
        if "score_weights" not in source
        else dict.fromkeys(DEFAULT_SCORE_WEIGHTS, 0.0)
    )
    aliases = {"quality": "evidence_quality", "evidence": "evidence_quality"}
    for raw_key, value in raw_weights.items():
        key = aliases.get(str(raw_key), str(raw_key))
        if key not in DEFAULT_SCORE_WEIGHTS:
            raise InvalidLiteratureSettingError(f"score_weights.{key}", "unsupported dimension")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidLiteratureSettingError(f"score_weights.{key}", "must be numeric") from exc
        if not math.isfinite(number) or number < 0:
            raise InvalidLiteratureSettingError(
                f"score_weights.{key}", "must be finite and non-negative"
            )
        weights[key] = number
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


def _credential_entry(source: str, item: Any, index: int) -> dict[str, Any] | None:
    """Normalize legacy encrypted strings and current credential objects."""

    if isinstance(item, str) and item:
        stable = uuid.uuid5(uuid.NAMESPACE_URL, f"polaris:{source}:{index}:{item}")
        return {
            "id": str(stable),
            "secret": item,
            "enabled": True,
            "label": None,
            "health": None,
            "created_at": None,
            "updated_at": None,
        }
    if not isinstance(item, Mapping) or not item.get("secret"):
        return None
    credential_id = str(item.get("id") or uuid.uuid4())
    try:
        uuid.UUID(credential_id)
    except ValueError:
        credential_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"polaris:{source}:{credential_id}"))
    return {
        "id": credential_id,
        "secret": str(item["secret"]),
        "enabled": bool(item.get("enabled", True)),
        "label": str(item.get("label") or "").strip() or None,
        "health": dict(item.get("health") or {}) or None,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _credential_pools(value: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    keys = value.get("provider_keys") if isinstance(value, Mapping) else {}
    pools: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(keys, Mapping):
        return pools
    for source, items in keys.items():
        if not isinstance(items, list):
            continue
        normalized = [
            entry
            for index, item in enumerate(items)
            if (entry := _credential_entry(str(source), item, index)) is not None
        ]
        if normalized:
            pools[str(source)] = normalized
    return pools


def _masked(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, list[dict[str, Any]]] = {}
    for source, pool in _credential_pools(value).items():
        result[source] = [
            {
                "id": item["id"],
                "source": source,
                "index": index,
                "configured": True,
                "preview": _mask(decrypt_secret(item["secret"])),
                "enabled": item["enabled"],
                "label": item["label"],
                "health": item["health"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for index, item in enumerate(pool)
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
    pools: dict[str, list[str]] = {}
    for source, items in _credential_pools(value).items():
        pools[source] = [decrypt_secret(item["secret"]) for item in items if item["enabled"]]
    normalized["provider_keys"] = pools
    return normalized


async def update_settings(session: AsyncSession, data: Mapping[str, Any]) -> dict[str, Any]:
    row = await _setting_for_update(session)
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
            now = time.time()
            encrypted[source_name] = [
                {
                    "id": str(uuid.uuid4()),
                    "secret": encrypt_secret(str(item).strip()),
                    "enabled": True,
                    "label": None,
                    "health": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for item in values
            ]
        normalized["provider_keys"] = encrypted
    else:
        normalized["provider_keys"] = (
            dict(previous.get("provider_keys") or {}) if isinstance(previous, Mapping) else {}
        )
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=normalized))
    else:
        row.value = normalized
    await session.commit()
    return await get_settings(session)


def _validate_credential_source(source: str) -> str:
    source = source.strip().lower()
    if source not in SUPPORTED_SOURCES:
        raise InvalidLiteratureSettingError("source", f"unsupported source: {source}")
    if source == "arxiv":
        raise InvalidLiteratureSettingError("source", "arxiv does not require credentials")
    return source


async def create_provider_credential(
    session: AsyncSession,
    *,
    source: str,
    secret: str,
    label: str | None,
    enabled: bool,
) -> dict[str, Any]:
    source = _validate_credential_source(source)
    secret = secret.strip()
    if not secret:
        raise InvalidLiteratureSettingError("secret", "must not be empty")
    row = await _setting_for_update(session)
    value = dict(row.value) if row is not None and isinstance(row.value, Mapping) else {}
    pools = _credential_pools(value)
    now = time.time()
    entry = {
        "id": str(uuid.uuid4()),
        "secret": encrypt_secret(secret),
        "enabled": enabled,
        "label": str(label or "").strip() or None,
        "health": None,
        "created_at": now,
        "updated_at": now,
    }
    pools.setdefault(source, []).append(entry)
    value["provider_keys"] = pools
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return next(item for item in _masked(value)[source] if item["id"] == entry["id"])


async def update_provider_credential(
    session: AsyncSession,
    credential_id: str,
    *,
    secret: str | None = None,
    label: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    row = await _setting_for_update(session)
    if row is None or not isinstance(row.value, Mapping):
        return None
    value = dict(row.value)
    pools = _credential_pools(value)
    for source, entries in pools.items():
        for entry in entries:
            if entry["id"] != credential_id:
                continue
            if secret is not None:
                if not secret.strip():
                    raise InvalidLiteratureSettingError("secret", "must not be empty")
                entry["secret"] = encrypt_secret(secret.strip())
            if label is not None:
                entry["label"] = label.strip() or None
            if enabled is not None:
                entry["enabled"] = enabled
            entry["updated_at"] = time.time()
            value["provider_keys"] = pools
            row.value = value
            await session.commit()
            return next(item for item in _masked(value)[source] if item["id"] == credential_id)
    return None


async def delete_provider_credential(session: AsyncSession, credential_id: str) -> bool:
    row = await _setting_for_update(session)
    if row is None or not isinstance(row.value, Mapping):
        return False
    value = dict(row.value)
    pools = _credential_pools(value)
    found = False
    for source, entries in list(pools.items()):
        kept = [entry for entry in entries if entry["id"] != credential_id]
        found = found or len(kept) != len(entries)
        if kept:
            pools[source] = kept
        else:
            pools.pop(source, None)
    if not found:
        return False
    value["provider_keys"] = pools
    row.value = value
    await session.commit()
    return True


async def get_provider_credential_secret(
    session: AsyncSession, credential_id: str
) -> tuple[str, str] | None:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    for source, entries in _credential_pools(value).items():
        for entry in entries:
            if entry["id"] == credential_id:
                return source, decrypt_secret(entry["secret"])
    return None


async def record_credential_health(
    session: AsyncSession, credential_id: str, *, ok: bool, detail: str
) -> None:
    row = await _setting_for_update(session)
    if row is None or not isinstance(row.value, Mapping):
        return
    value = dict(row.value)
    pools = _credential_pools(value)
    for entries in pools.values():
        for entry in entries:
            if entry["id"] == credential_id:
                entry["health"] = {
                    "ok": ok,
                    "detail": detail[:500],
                    "checked_at": time.time(),
                }
                entry["updated_at"] = time.time()
                value["provider_keys"] = pools
                row.value = value
                await session.commit()
                return


async def record_provider_health(
    session: AsyncSession, *, source: str, ok: bool, detail: str
) -> None:
    row = await _setting_for_update(session)
    value = dict(row.value) if row is not None and isinstance(row.value, Mapping) else {}
    health = dict(value.get("provider_health") or {})
    health[source] = {"ok": ok, "detail": detail[:500], "checked_at": time.time()}
    value["provider_health"] = health
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
