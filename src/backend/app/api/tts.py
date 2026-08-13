"""Authenticated speech synthesis and personal playback settings."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.tts import TTSSpeechRequest, TTSUserSettingsRead, TTSUserSettingsUpdate
from app.services import tts as tts_service

router = APIRouter(prefix="/tts", tags=["tts"])


def _read_settings(data: dict) -> TTSUserSettingsRead:
    return TTSUserSettingsRead(**data)


@router.get("/settings", response_model=TTSUserSettingsRead)
async def get_my_tts_settings(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> TTSUserSettingsRead:
    _, effective = await tts_service.effective_settings(session, user)
    return _read_settings(effective)


@router.put("/settings", response_model=TTSUserSettingsRead)
async def set_my_tts_settings(
    payload: TTSUserSettingsUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> TTSUserSettingsRead:
    try:
        effective = await tts_service.set_user_settings(session, user, payload.model_dump())
    except tts_service.InvalidTTSSettingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_TTS_SETTING:{exc.field}"
        ) from exc
    return _read_settings(effective)


@router.post("/speech")
async def synthesize_speech(
    payload: TTSSpeechRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    try:
        path = await tts_service.synthesize_to_cache(
            session, user=user, source=payload.text, context=payload.context
        )
    except tts_service.TTSNotAvailableError as exc:
        detail = str(exc)
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if detail.startswith(
            "TTS_TEXT_TOO_LONG"
        ) else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(code, detail=detail) from exc
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"polaris-{payload.context}.wav",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/speech/stream")
async def stream_speech(
    payload: TTSSpeechRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> StreamingResponse:
    try:
        stream = await tts_service.open_speech_stream(
            session, user=user, source=payload.text
        )
    except tts_service.TTSNotAvailableError as exc:
        detail = str(exc)
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if detail.startswith(
            "TTS_TEXT_TOO_LONG"
        ) else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(code, detail=detail) from exc
    return StreamingResponse(
        stream.content,
        media_type="audio/pcm",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Audio-Sample-Rate": str(stream.sample_rate),
            "X-Audio-Channels": "1",
            "X-Audio-Sample-Format": "s16le",
            "X-Audio-Playback-Rate": str(stream.playback_rate),
        },
    )
