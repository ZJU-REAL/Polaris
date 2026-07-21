"""管理端 LLM 配置 schema（docs/api-m1.md §2）。"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderKind = Literal["openai_compat", "anthropic", "fake"]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: ProviderKind
    base_url: str | None = None
    api_key: str | None = None  # 只写不读；入库前 Fernet 加密
    enabled: bool = True
    models: list[str] | None = None  # 可用模型 id 列表（None = 未配置）


class ProviderUpdate(BaseModel):
    name: str | None = None
    kind: ProviderKind | None = None
    base_url: str | None = None
    api_key: str | None = None  # 空字符串 = 不变
    enabled: bool | None = None
    models: list[str] | None = None  # 整体替换；None = 不变（清空传 []）


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    base_url: str | None
    api_key_masked: str
    enabled: bool
    models: list[str] | None = None


class RouteItem(BaseModel):
    stage: str
    provider_id: uuid.UUID
    model: str = Field(min_length=1, max_length=255)
    temperature: float | None = None  # None = 用 provider 默认


class UsageRow(BaseModel):
    date: str
    stage: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    calls: int
