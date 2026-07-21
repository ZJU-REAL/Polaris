"""管理端 LLM 配置 schema（docs/api-m1.md §2）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

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


TestCapability = Literal["chat", "embedding", "rerank"]


class TestModelRequest(BaseModel):
    """模型连通性测试：按 provider 直连探测（不经过路由表，不记账、不写调用日志）。"""

    provider_id: uuid.UUID
    model: str = Field(min_length=1, max_length=255)
    capability: TestCapability = "chat"


class TestModelResult(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None


class UsageRow(BaseModel):
    date: str
    stage: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    calls: int


# ---- 调用日志 ----


class CallLogSettings(BaseModel):
    """调用日志开关（系统级，默认关）。"""

    enabled: bool


class CallLogRow(BaseModel):
    """列表行：request/response 只给截断预览，全文走详情端点。"""

    id: uuid.UUID
    created_at: datetime
    stage: str
    provider_name: str
    model: str
    duration_ms: int
    status: str  # ok|error
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    voyage_id: uuid.UUID | None
    request_preview: str
    response_preview: str


class CallLogPage(BaseModel):
    total: int
    items: list[CallLogRow]


class CallLogDetail(BaseModel):
    id: uuid.UUID
    created_at: datetime
    stage: str
    provider_name: str
    model: str
    duration_ms: int
    status: str
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    voyage_id: uuid.UUID | None
    request: Any | None  # {"messages": [{role, content}], "images": ["[image ~N KB]"]} 或摘要
    response: str | None


class LlmManagedStatus(BaseModel):
    """用户 LLM 接管状态：True=自管，False=被管理员接管。"""

    self_managed: bool


class LlmSelfConfig(BaseModel):
    """当前生效的 LLM 配置（provider key 掩码），供用户端只读展示。"""

    self_managed: bool
    providers: list[ProviderRead]
    routes: list[RouteItem]


class EffectiveTestRequest(BaseModel):
    """测试当前生效路由：按 stage 探测用户实际会用到的 provider+model。"""

    stage: str


class EffectiveTestResult(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None
    model: str
    provider_name: str
    # 生效 provider 是内置 fake（未配置真实模型时的回退）
    is_fake: bool = False
