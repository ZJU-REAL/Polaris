"""Persistent administrator settings for PDF document processing.

MinerU credentials are encrypted at rest and are only decrypted when a worker
resolves the runtime configuration for a concrete parsing attempt.
"""

from __future__ import annotations

import time
import uuid
import zlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings as get_app_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models.system_setting import SystemSetting
from app.services.mineru import MineruRuntimeConfig

SETTING_KEY = "document_processing"
_SETTING_LOCK_ID = zlib.crc32(SETTING_KEY.encode("utf-8"))


class InvalidDocumentProcessingSettingError(ValueError):
    """A document-processing setting failed validation."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field


def _environment_defaults() -> dict[str, Any]:
    settings = get_app_settings()
    return {
        "mineru_enabled": True,
        "mineru_base_url": settings.mineru_base_url,
        "mineru_timeout_seconds": settings.mineru_timeout_seconds,
        "mineru_poll_interval_seconds": settings.mineru_poll_interval_seconds,
        "mineru_retries": settings.mineru_retries,
        "mineru_concurrency": settings.mineru_concurrency,
        "pymupdf_fallback_enabled": True,
    }


async def _setting_for_update(session: AsyncSession) -> SystemSetting | None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _SETTING_LOCK_ID}
        )
    return await session.scalar(
        select(SystemSetting).where(SystemSetting.key == SETTING_KEY).with_for_update()
    )


def _as_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise InvalidDocumentProcessingSettingError(field, "must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDocumentProcessingSettingError(field, "must be an integer") from exc
    if not minimum <= result <= maximum:
        raise InvalidDocumentProcessingSettingError(
            field, f"must be between {minimum} and {maximum}"
        )
    return result


def _as_float(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise InvalidDocumentProcessingSettingError(field, "must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDocumentProcessingSettingError(field, "must be numeric") from exc
    if not minimum <= result <= maximum:
        raise InvalidDocumentProcessingSettingError(
            field, f"must be between {minimum:g} and {maximum:g}"
        )
    return result


def _base_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidDocumentProcessingSettingError(
            "mineru_base_url", "must be an HTTP(S) API root without credentials or query"
        )
    return url


def _normalize(value: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults = _environment_defaults()
    source = {**defaults, **dict(value or {})}
    normalized = {
        "mineru_enabled": bool(source["mineru_enabled"]),
        "mineru_base_url": _base_url(source["mineru_base_url"]),
        "mineru_timeout_seconds": _as_float(
            source["mineru_timeout_seconds"],
            "mineru_timeout_seconds",
            minimum=31,
            maximum=86_400,
        ),
        "mineru_poll_interval_seconds": _as_float(
            source["mineru_poll_interval_seconds"],
            "mineru_poll_interval_seconds",
            minimum=1,
            maximum=300,
        ),
        "mineru_retries": _as_int(source["mineru_retries"], "mineru_retries", minimum=0, maximum=5),
        "mineru_concurrency": _as_int(
            source["mineru_concurrency"], "mineru_concurrency", minimum=1, maximum=16
        ),
        "pymupdf_fallback_enabled": bool(source["pymupdf_fallback_enabled"]),
    }
    if not normalized["mineru_enabled"] and not normalized["pymupdf_fallback_enabled"]:
        raise InvalidDocumentProcessingSettingError(
            "parser_policy", "at least one parser must be enabled"
        )
    return normalized


def _credential_entry(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str) and item:
        stable = uuid.uuid5(uuid.NAMESPACE_URL, f"polaris:mineru:{index}:{item}")
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
        credential_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"polaris:mineru:{credential_id}"))
    return {
        "id": credential_id,
        "secret": str(item["secret"]),
        "enabled": bool(item.get("enabled", True)),
        "label": str(item.get("label") or "").strip() or None,
        "health": dict(item.get("health") or {}) or None,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _credential_pool(value: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    raw = value.get("mineru_credentials") if isinstance(value, Mapping) else None
    if not isinstance(raw, list):
        return []
    return [
        entry
        for index, item in enumerate(raw)
        if (entry := _credential_entry(item, index)) is not None
    ]


def _mask(secret: str) -> str:
    return f"••••{secret[-4:]}" if len(secret) >= 4 else "••••"


def _masked_credentials(value: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": entry["id"],
            "provider": "mineru",
            "index": index,
            "configured": True,
            "preview": _mask(decrypt_secret(entry["secret"])),
            "enabled": entry["enabled"],
            "label": entry["label"],
            "health": entry["health"],
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
        }
        for index, entry in enumerate(_credential_pool(value))
    ]


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    return {
        **_normalize(value),
        "mineru_credentials": _masked_credentials(value),
    }


async def get_runtime_config(session: AsyncSession) -> MineruRuntimeConfig:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    normalized = _normalize(value)
    if "mineru_credentials" in value:
        tokens = [
            decrypt_secret(entry["secret"]) for entry in _credential_pool(value) if entry["enabled"]
        ]
    else:
        tokens = [
            item.strip()
            for item in get_app_settings().mineru_api_tokens.replace(";", ",").split(",")
            if item.strip()
        ]
    return MineruRuntimeConfig(
        **normalized,
        mineru_api_tokens=tuple(tokens),
    )


async def update_settings(session: AsyncSession, data: Mapping[str, Any]) -> dict[str, Any]:
    row = await _setting_for_update(session)
    previous = dict(row.value) if row is not None and isinstance(row.value, Mapping) else {}
    normalized = _normalize({**previous, **dict(data)})
    value = dict(normalized)
    if "mineru_credentials" in previous:
        value["mineru_credentials"] = previous["mineru_credentials"]
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return await get_settings(session)


async def create_credential(
    session: AsyncSession, *, secret: str, label: str | None, enabled: bool
) -> dict[str, Any]:
    secret = secret.strip()
    if not secret:
        raise InvalidDocumentProcessingSettingError("secret", "must not be empty")
    row = await _setting_for_update(session)
    value = dict(row.value) if row is not None and isinstance(row.value, Mapping) else {}
    pool = _credential_pool(value)
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
    pool.append(entry)
    value["mineru_credentials"] = pool
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return next(item for item in _masked_credentials(value) if item["id"] == entry["id"])


async def update_credential(
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
    pool = _credential_pool(value)
    for entry in pool:
        if entry["id"] != credential_id:
            continue
        if secret is not None:
            if not secret.strip():
                raise InvalidDocumentProcessingSettingError("secret", "must not be empty")
            entry["secret"] = encrypt_secret(secret.strip())
        if label is not None:
            entry["label"] = label.strip() or None
        if enabled is not None:
            entry["enabled"] = enabled
        entry["updated_at"] = time.time()
        value["mineru_credentials"] = pool
        row.value = value
        await session.commit()
        return next(item for item in _masked_credentials(value) if item["id"] == credential_id)
    return None


async def delete_credential(session: AsyncSession, credential_id: str) -> bool:
    row = await _setting_for_update(session)
    if row is None or not isinstance(row.value, Mapping):
        return False
    value = dict(row.value)
    pool = _credential_pool(value)
    kept = [entry for entry in pool if entry["id"] != credential_id]
    if len(kept) == len(pool):
        return False
    value["mineru_credentials"] = kept
    row.value = value
    await session.commit()
    return True


async def get_credential_secret(session: AsyncSession, credential_id: str) -> str | None:
    row = await session.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, Mapping) else {}
    for entry in _credential_pool(value):
        if entry["id"] == credential_id:
            return decrypt_secret(entry["secret"])
    return None


async def record_credential_health(
    session: AsyncSession, credential_id: str, *, ok: bool, detail: str
) -> None:
    row = await _setting_for_update(session)
    if row is None or not isinstance(row.value, Mapping):
        return
    value = dict(row.value)
    pool = _credential_pool(value)
    for entry in pool:
        if entry["id"] != credential_id:
            continue
        entry["health"] = {
            "ok": ok,
            "detail": detail[:500],
            "checked_at": time.time(),
        }
        entry["updated_at"] = time.time()
        value["mineru_credentials"] = pool
        row.value = value
        await session.commit()
        return


async def probe_mineru_credential(
    *,
    base_url: str,
    secret: str,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Authenticate with an impossible batch id without consuming parse quota."""

    started = time.perf_counter()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=min(timeout_seconds, 30.0))
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/extract-results/batch/polaris-connectivity-probe",
            headers={"Authorization": f"Bearer {secret}"},
        )
        status_code = response.status_code
        ok = 200 <= status_code < 400 or status_code == 404
        detail = (
            "MinerU credential accepted"
            if ok
            else f"MinerU authentication failed (HTTP_{status_code})"
            if status_code in {401, 403}
            else f"MinerU probe failed (HTTP_{status_code})"
        )
        return {
            "provider": "mineru",
            "ok": ok,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "status_code": status_code,
            "detail": detail,
        }
    except httpx.TimeoutException:
        detail = "MinerU probe timed out"
    except httpx.RequestError:
        detail = "MinerU network request failed"
    finally:
        if owns_client:
            await http.aclose()
    return {
        "provider": "mineru",
        "ok": False,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "status_code": None,
        "detail": detail,
    }
