"""Idempotent translation lifecycle for discovery-hit metadata."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.models.literature_discovery import LiteratureHitTranslation, LiteratureSearchHit

TRANSLATION_STAGE = "translation"
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class TranslationRouter(Protocol):
    async def model_name(self, stage: str, user_id: uuid.UUID | None = None) -> str | None: ...

    async def complete(self, stage: str, messages: Sequence[Message], **kwargs: Any): ...


class InvalidTargetLanguageError(ValueError):
    """The requested target language is not a bounded BCP-47-style tag."""


class TranslationOutputError(ValueError):
    """The model response does not satisfy the translation JSON contract."""


def normalize_target_language(value: str) -> str:
    language = value.strip()
    if not _LANGUAGE_RE.fullmatch(language):
        raise InvalidTargetLanguageError("INVALID_TARGET_LANGUAGE")
    parts = language.split("-")
    return "-".join([parts[0].lower(), *(part.lower() for part in parts[1:])])


def source_fields(hit: LiteratureSearchHit) -> dict[str, Any]:
    scores = hit.scores if isinstance(hit.scores, Mapping) else {}
    reasons = scores.get("reasons")
    if isinstance(reasons, list):
        rationale = [str(item) for item in reasons if str(item).strip()]
    else:
        value = scores.get("rationale") or scores.get("reason")
        rationale = [str(value)] if value else []
    return {
        "title": hit.title,
        "abstract": hit.abstract,
        "inclusion_rationale": rationale,
    }


def source_hash(hit: LiteratureSearchHit) -> str:
    payload = json.dumps(
        source_fields(hit), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def model_version(llm: TranslationRouter, user_id: uuid.UUID | None) -> str:
    resolve = getattr(llm, "resolve", None)
    if callable(resolve):
        _, route = await resolve(TRANSLATION_STAGE, user_id)
        provider = str(getattr(route, "provider_name", "") or "default")
        model = str(getattr(route, "model", "") or "")
        if model:
            return f"{provider}:{model}"[:255]
    model = await llm.model_name(TRANSLATION_STAGE, user_id)
    if not model:
        raise TranslationOutputError("TRANSLATION_MODEL_UNAVAILABLE")
    return str(model)[:255]


async def request_translation(
    session: AsyncSession,
    *,
    hit: LiteratureSearchHit,
    target_language: str,
    model: str,
) -> tuple[LiteratureHitTranslation, bool]:
    language = normalize_target_language(target_language)
    digest = source_hash(hit)
    lookup = (
        LiteratureHitTranslation.hit_id == hit.id,
        LiteratureHitTranslation.target_language == language,
        LiteratureHitTranslation.source_hash == digest,
        LiteratureHitTranslation.model_version == model,
    )
    row = await session.scalar(select(LiteratureHitTranslation).where(*lookup))
    if row is not None:
        should_enqueue = row.status == "failed"
        if should_enqueue:
            row.status = "queued"
            row.error_code = None
            row.started_at = None
            row.completed_at = None
        await session.commit()
        await session.refresh(row)
        return row, should_enqueue

    row = LiteratureHitTranslation(
        hit_id=hit.id,
        target_language=language,
        source_hash=digest,
        model_version=model,
        status="queued",
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        row = await session.scalar(select(LiteratureHitTranslation).where(*lookup))
        if row is None:
            raise
        await session.commit()
        return row, False
    await session.commit()
    await session.refresh(row)
    return row, True


async def get_translation(
    session: AsyncSession,
    *,
    hit_id: uuid.UUID,
    target_language: str,
    model: str | None = None,
) -> LiteratureHitTranslation | None:
    statement = select(LiteratureHitTranslation).where(
        LiteratureHitTranslation.hit_id == hit_id,
        LiteratureHitTranslation.target_language == normalize_target_language(target_language),
    )
    if model is not None:
        statement = statement.where(LiteratureHitTranslation.model_version == model)
    return await session.scalar(statement.order_by(LiteratureHitTranslation.created_at.desc()))


async def mark_dispatch_failed(
    session: AsyncSession, translation_id: uuid.UUID
) -> LiteratureHitTranslation | None:
    row = await session.get(LiteratureHitTranslation, translation_id)
    if row is None or row.status != "queued":
        return row
    row.status = "failed"
    row.error_code = "QUEUE_DISPATCH_FAILED"
    row.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return row


def _json_object(value: str) -> Mapping[str, Any]:
    text = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslationOutputError("TRANSLATION_OUTPUT_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise TranslationOutputError("TRANSLATION_OUTPUT_INVALID")
    return payload


def parse_translation(value: str, source: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_object(value)
    title = payload.get("title")
    abstract = payload.get("abstract")
    rationale = payload.get("inclusion_rationale")
    if not isinstance(title, str) or not title.strip():
        raise TranslationOutputError("TRANSLATION_OUTPUT_INVALID")
    if source.get("abstract") is not None and not isinstance(abstract, str):
        raise TranslationOutputError("TRANSLATION_OUTPUT_INVALID")
    if source.get("abstract") is None:
        abstract = None
    if not isinstance(rationale, list) or not all(isinstance(item, str) for item in rationale):
        raise TranslationOutputError("TRANSLATION_OUTPUT_INVALID")
    return {
        "title": title.strip(),
        "abstract": abstract.strip() if isinstance(abstract, str) else None,
        "inclusion_rationale": [item.strip() for item in rationale if item.strip()],
    }


def stable_error_code(exc: Exception) -> str:
    if isinstance(exc, TranslationOutputError):
        return str(exc)[:64]
    name = type(exc).__name__.upper()
    if "TIMEOUT" in name:
        return "TRANSLATION_TIMEOUT"
    if "NOTCONFIGURED" in name or "NOT_CONFIGURED" in name:
        return "TRANSLATION_MODEL_UNAVAILABLE"
    return "TRANSLATION_FAILED"


async def execute_translation(
    session: AsyncSession,
    *,
    translation_id: uuid.UUID,
    llm: TranslationRouter,
    user_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> LiteratureHitTranslation | None:
    row = await session.get(LiteratureHitTranslation, translation_id, with_for_update=True)
    if row is None or row.status != "queued":
        return row
    row.status = "running"
    row.attempt_count += 1
    row.started_at = datetime.now(UTC)
    row.completed_at = None
    await session.commit()

    hit = await session.get(LiteratureSearchHit, row.hit_id)
    if hit is None:
        row.status = "failed"
        row.error_code = "TRANSLATION_HIT_NOT_FOUND"
        row.completed_at = datetime.now(UTC)
        await session.commit()
        return row
    try:
        current_model = await model_version(llm, user_id)
        if current_model != row.model_version:
            raise TranslationOutputError("TRANSLATION_MODEL_CHANGED")
        source = source_fields(hit)
        result = await llm.complete(
            TRANSLATION_STAGE,
            [
                Message(
                    role="system",
                    content=(
                        "Translate scholarly discovery metadata faithfully. Return JSON only with "
                        "title, abstract, and inclusion_rationale. Preserve technical symbols, "
                        "numbers, DOI values, and proper nouns. Do not add claims."
                    ),
                ),
                Message(
                    role="user",
                    content=json.dumps(
                        {"target_language": row.target_language, "source": source},
                        ensure_ascii=False,
                    ),
                ),
            ],
            temperature=0.0,
            max_tokens=4000,
            user_id=user_id,
            library_id=library_id,
        )
        translated = parse_translation(result.content, source)
    except Exception as exc:
        row.status = "failed"
        row.error_code = stable_error_code(exc)
        row.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        return row

    row.status = "ready"
    row.translated_fields = translated
    row.error_code = None
    row.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return row
