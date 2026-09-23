"""管理端 LLM 配置 schema（docs/task-system.md §7（原 api-m1.md §2））。"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.llm.base import EffortLevel

#: 协议由配置的人显式选，不按模型名猜：同一个模型在不同网关上可能暴露不同接口（#809）。
ProviderKind = Literal["openai_compat", "openai_responses", "anthropic", "fake"]
UserAgent = Annotated[str, Field(max_length=255, pattern=r"^[^\r\n]*$")]
#: rerank 端点路径。必须以 / 开头：它是接在 base_url 后面的路径，不是完整 URL；
#: 不带斜杠会拼成 ``https://host/v1rerank`` 这种既不报错也永远打不通的地址。
RerankPath = Annotated[str, Field(max_length=128, pattern=r"^/[^\s?#]*$")]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: ProviderKind
    base_url: str | None = None
    user_agent: UserAgent | None = None
    api_key: str | None = None  # 只写不读；入库前 Fernet 加密
    enabled: bool = True
    models: list[str] | None = None  # 可用模型 id 列表（None = 未配置）
    rerank_path: RerankPath | None = None  # None = 默认 /rerank（仅 openai_compat 用）


class ProviderUpdate(BaseModel):
    name: str | None = None
    kind: ProviderKind | None = None
    base_url: str | None = None
    user_agent: UserAgent | None = None  # 空字符串 = 恢复 HTTP 客户端默认值
    api_key: str | None = None  # 空字符串 = 不变
    enabled: bool | None = None
    models: list[str] | None = None  # 整体替换；None = 不变（清空传 []）
    rerank_path: RerankPath | None = None  # None = 不变；恢复默认传 "/rerank"


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    base_url: str | None
    user_agent: str | None
    api_key_masked: str
    enabled: bool
    models: list[str] | None = None
    rerank_path: str | None = None


class RouteItem(BaseModel):
    stage: str
    provider_id: uuid.UUID
    model: str = Field(min_length=1, max_length=255)
    temperature: float | None = None  # None = 用 provider 默认
    # 推理档位；None = 不发送该参数（用模型默认）。某个模型具体支持哪几档由服务端校验，
    # 这里只挡明显非法的取值。
    effort: EffortLevel | None = None
    # 模型的上下文窗口（token）。以前库里有这一列、API 没有这个字段，而 PUT 是整表
    # 覆盖——于是每保存一次路由表，填过的窗口就被悄悄清空（#811）。
    context_window: int | None = Field(default=None, ge=1024, le=10_000_000)
    # 输入预算覆盖（键 → 字符数）；键与上下限见 core/llm/budgets.py，服务端校验
    input_budgets: dict[str, int] | None = None


class InputBudgetSpec(BaseModel):
    """一个可调输入预算的声明，设置页据此画输入框、给出默认值与上下限。"""

    stage: str
    key: str
    default: int
    minimum: int
    maximum: int
    #: 窗口 token → 预算字符上限的换算系数（见 budgets.CHARS_PER_WINDOW_TOKEN）
    chars_per_window_token: int


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
