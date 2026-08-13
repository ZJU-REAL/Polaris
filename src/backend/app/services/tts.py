"""TTS configuration, text normalization, upstream synthesis, and WAV cache."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings as get_app_settings
from app.models.system_setting import SystemSetting
from app.models.user import User

logger = logging.getLogger(__name__)

SETTING_KEY = "tts_config"
USER_SETTING_KEY = "tts"
_WAV_LIMIT_BYTES = 128 * 1024 * 1024
_DEFAULT_ADMIN_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "provider": "openai_compatible",
    "base_url": "http://host.docker.internal:50000/v1",
    "model": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
    "default_voice": "default",
    "default_speed": 1.0,
    "max_chars": 20_000,
}
_cache_locks: dict[str, asyncio.Lock] = {}
_cache_lock_users: dict[str, int] = {}


class InvalidTTSSettingError(ValueError):
    def __init__(self, field: str, value: object) -> None:
        super().__init__(f"{field}={value!r}")
        self.field = field
        self.value = value


class TTSNotAvailableError(RuntimeError):
    pass


def _defaults() -> dict[str, Any]:
    # Runtime configuration is database-backed and managed from the admin UI.
    # These values only make the empty form useful on a fresh installation.
    return dict(_DEFAULT_ADMIN_SETTINGS)


def _clean_system(raw: Any) -> dict[str, Any]:
    out = _defaults()
    if isinstance(raw, dict):
        for key in out:
            if key in raw:
                out[key] = raw[key]
    out["enabled"] = bool(out["enabled"])
    out["provider"] = "openai_compatible"
    out["base_url"] = str(out["base_url"] or "").strip().rstrip("/")
    out["model"] = str(out["model"] or "").strip()
    out["default_voice"] = str(out["default_voice"] or "default").strip()
    try:
        out["default_speed"] = float(out["default_speed"])
    except (TypeError, ValueError):
        out["default_speed"] = 1.0
    try:
        out["max_chars"] = int(out["max_chars"])
    except (TypeError, ValueError):
        out["max_chars"] = 20_000
    return out


def validate_system(raw: Any) -> dict[str, Any]:
    out = _clean_system(raw)
    parsed = urlsplit(out["base_url"])
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidTTSSettingError("base_url", out["base_url"])
    if not out["model"] or len(out["model"]) > 200 or any(c.isspace() for c in out["model"]):
        raise InvalidTTSSettingError("model", out["model"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", out["default_voice"]):
        raise InvalidTTSSettingError("default_voice", out["default_voice"])
    if not 0.5 <= out["default_speed"] <= 2.0:
        raise InvalidTTSSettingError("default_speed", out["default_speed"])
    if not 200 <= out["max_chars"] <= 50_000:
        raise InvalidTTSSettingError("max_chars", out["max_chars"])
    return out


async def get_admin_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(SystemSetting, SETTING_KEY)
    return _clean_system(row.value if row is not None else None)


async def set_admin_settings(session: AsyncSession, raw: Any) -> dict[str, Any]:
    cleaned = validate_system(raw)
    row = await session.get(SystemSetting, SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=cleaned))
    else:
        row.value = cleaned
    await session.commit()
    return cleaned


def _user_settings(user: User) -> dict[str, Any]:
    raw = (user.settings or {}).get(USER_SETTING_KEY)
    if not isinstance(raw, dict):
        return {"enabled": True, "model": None, "voice": None, "speed": None}
    speed = raw.get("speed")
    try:
        speed = float(speed) if speed is not None else None
    except (TypeError, ValueError):
        speed = None
    return {
        "enabled": bool(raw.get("enabled", True)),
        "model": str(raw["model"]).strip() if raw.get("model") else None,
        "voice": str(raw["voice"]).strip() if raw.get("voice") else None,
        "speed": speed if speed is not None and 0.5 <= speed <= 2.0 else None,
    }


async def effective_settings(
    session: AsyncSession, user: User
) -> tuple[dict[str, Any], dict[str, Any]]:
    admin = await get_admin_settings(session)
    own = _user_settings(user)
    # This adapter advertises one administrator-approved model and voice. A
    # stale personal override follows the new default instead of breaking play.
    model = own["model"] if own["model"] == admin["model"] else admin["model"]
    voice = own["voice"] if own["voice"] == admin["default_voice"] else admin["default_voice"]
    return admin, {
        **own,
        "available": bool(admin["enabled"] and own["enabled"]),
        "effective_model": model,
        "effective_voice": voice,
        "effective_speed": own["speed"] or admin["default_speed"],
        "available_models": [admin["model"]],
        "available_voices": [admin["default_voice"]],
        "max_chars": admin["max_chars"],
    }


async def set_user_settings(
    session: AsyncSession, user: User, raw: Any
) -> dict[str, Any]:
    admin = await get_admin_settings(session)
    data = raw if isinstance(raw, dict) else {}
    model = str(data.get("model") or "").strip() or None
    voice = str(data.get("voice") or "").strip() or None
    if model is not None and model != admin["model"]:
        raise InvalidTTSSettingError("model", model)
    if voice is not None and voice != admin["default_voice"]:
        raise InvalidTTSSettingError("voice", voice)
    speed = data.get("speed")
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError) as exc:
            raise InvalidTTSSettingError("speed", speed) from exc
        if not 0.5 <= speed <= 2.0:
            raise InvalidTTSSettingError("speed", speed)
    saved = {
        "enabled": bool(data.get("enabled", True)),
        "model": model,
        "voice": voice,
        "speed": speed,
    }
    user.settings = {**(user.settings or {}), USER_SETTING_KEY: saved}
    await session.commit()
    _, effective = await effective_settings(session, user)
    return effective


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]")
_CITATION_RE = re.compile(r"(?<!\w)\[\d{1,3}\]")


def text_for_speech(source: str) -> str:
    """Turn display Markdown into natural plain text without an LLM call."""
    text = html.unescape(source).replace("\r\n", "\n")
    text = _CODE_FENCE_RE.sub("\n代码块已略过。\n", text)
    text = re.sub(r"!\[\[[^\]]+\]\]", "", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _WIKI_LINK_RE.sub(r"\1", text)
    text = _CITATION_RE.sub("", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\|", "，", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


async def _request_audio(config: dict[str, Any], text: str) -> bytes:
    timeout = httpx.Timeout(connect=10, read=600, write=30, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{config['base_url']}/audio/speech",
                json={
                    "model": config["effective_model"],
                    "input": text,
                    "voice": config["effective_voice"],
                    "speed": config["effective_speed"],
                    "response_format": "wav",
                },
            )
    except httpx.HTTPError as exc:
        raise TTSNotAvailableError(f"TTS_UPSTREAM_UNREACHABLE:{type(exc).__name__}") from exc
    if response.status_code >= 400:
        logger.warning(
            "TTS upstream returned %d: %s", response.status_code, response.text[:500]
        )
        raise TTSNotAvailableError(f"TTS_UPSTREAM_ERROR:{response.status_code}")
    audio = response.content
    if not audio.startswith(b"RIFF") or b"WAVE" not in audio[:16]:
        raise TTSNotAvailableError("TTS_INVALID_AUDIO")
    if len(audio) > _WAV_LIMIT_BYTES:
        raise TTSNotAvailableError("TTS_AUDIO_TOO_LARGE")
    return audio


async def test_admin_settings(raw: Any) -> int:
    config = validate_system(raw)
    effective = {
        **config,
        "effective_model": config["model"],
        "effective_voice": config["default_voice"],
        "effective_speed": config["default_speed"],
    }
    return len(await _request_audio(effective, "Polaris 语音服务连接测试。"))


async def synthesize_to_cache(
    session: AsyncSession,
    *,
    user: User,
    source: str,
    context: str,
) -> Path:
    admin, effective = await effective_settings(session, user)
    if not effective["available"]:
        raise TTSNotAvailableError("TTS_DISABLED")
    text = text_for_speech(source)
    if not text:
        raise TTSNotAvailableError("TTS_EMPTY_TEXT")
    if len(text) > admin["max_chars"]:
        raise TTSNotAvailableError(f"TTS_TEXT_TOO_LONG:{admin['max_chars']}")

    identity = json.dumps(
        {
            "v": 1,
            "context": context,
            "model": effective["effective_model"],
            "voice": effective["effective_voice"],
            "speed": effective["effective_speed"],
            "text": text,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    key = hashlib.sha256(identity).hexdigest()
    cache_dir = Path(get_app_settings().data_dir) / "tts-cache"
    path = cache_dir / f"{key}.wav"
    if path.is_file():
        return path

    lock = _cache_locks.setdefault(key, asyncio.Lock())
    _cache_lock_users[key] = _cache_lock_users.get(key, 0) + 1
    try:
        async with lock:
            if path.is_file():
                return path
            audio = await _request_audio({**admin, **effective}, text)
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f".{key}.tmp"
            temp.write_bytes(audio)
            temp.replace(path)
    finally:
        # Reference counting keeps waiters on the same lock while ensuring
        # failed upstream requests do not leak one entry per unique text.
        remaining = _cache_lock_users[key] - 1
        if remaining == 0:
            _cache_lock_users.pop(key, None)
            _cache_locks.pop(key, None)
        else:
            _cache_lock_users[key] = remaining
    logger.info("cached %s TTS audio %s (%d bytes)", context, key[:12], path.stat().st_size)
    return path
