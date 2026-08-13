"""Speech synthesis configuration and request schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class TTSAdminSettings(BaseModel):
    enabled: bool = False
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    model: str
    default_voice: str = "default"
    default_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    max_chars: int = Field(default=20_000, ge=200, le=50_000)


class TTSTestResult(BaseModel):
    ok: bool
    model: str
    audio_bytes: int


class TTSVoicesResult(BaseModel):
    voices: list[str]
    sample_rate: int | None = None


class TTSUserSettingsUpdate(BaseModel):
    enabled: bool = True
    # None means "follow the administrator's current model".
    model: str | None = Field(default=None, max_length=200)
    voice: str | None = Field(default=None, max_length=64)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)


class TTSUserSettingsRead(BaseModel):
    enabled: bool
    available: bool
    model: str | None
    effective_model: str
    voice: str | None
    effective_voice: str
    speed: float | None
    effective_speed: float
    available_models: list[str]
    available_voices: list[str]
    max_chars: int


class TTSSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)
    context: Literal["assistant", "digest"] = "assistant"
