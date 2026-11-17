"""模型状态（只读）。

Polaris 只用 Claude，模型不可配置，key 与地址来自环境变量——所以这里没有任何写
接口，只有两样：现在用的是什么（``GET /llm/status``），以及现在连不连得上
（``POST /llm/status/check``，真的打一次最小的请求）。

返回里**永远没有 key**，地址也只给主机名：这两个接口的结果会原样显示在设置页上。
"""

import time
from typing import Literal

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import current_active_user
from app.core.config import get_settings
from app.core.llm.anthropic import AnthropicProvider
from app.models.user import User

router = APIRouter(prefix="/llm", tags=["llm"])

CheckError = Literal[
    "not_configured", "auth_failed", "unreachable", "model_not_found", "rate_limited", "failed"
]


class CapabilityStatus(BaseModel):
    model: str
    #: 能不能用：需要 key，并且配了提供这个模型的网关地址
    available: bool


class LLMStatus(BaseModel):
    #: 有没有 Claude 的 API key
    configured: bool
    model: str
    #: 请求发往的主机（host[:port]），不含路径与凭据
    base_host: str
    embedding: CapabilityStatus
    rerank: CapabilityStatus


class LLMCheckResult(BaseModel):
    ok: bool
    latency_ms: int | None = None
    error: CheckError | None = None
    #: 仅 error == "failed" 时给：服务端原话（截断、去掉 key），其余情况前端有固定文案
    detail: str | None = None


@router.get("/status", response_model=LLMStatus)
async def llm_status(_user: User = Depends(current_active_user)) -> LLMStatus:
    settings = get_settings()
    gateway = settings.claude_configured and settings.gateway_openai_base_url is not None
    return LLMStatus(
        configured=settings.claude_configured,
        model=settings.claude_model,
        base_host=settings.claude_base_host,
        embedding=CapabilityStatus(model=settings.embedding_model, available=gateway),
        rerank=CapabilityStatus(model=settings.rerank_model, available=gateway),
    )


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


@router.post("/status/check", response_model=LLMCheckResult)
async def check_llm(_user: User = Depends(current_active_user)) -> LLMCheckResult:
    """真的向 Claude 发一次最小的请求，告诉用户 key、地址、模型三样对不对。

    总是 200：连不上是「检查的结果」，不是这个接口出了错。不记账、不写调用日志。
    """
    settings = get_settings()
    if not settings.claude_configured:
        return LLMCheckResult(ok=False, error="not_configured")
    provider = AnthropicProvider(
        api_key=settings.claude_api_key,
        base_url=settings.claude_base_url or None,
        timeout=30.0,
        max_attempts=1,
    )
    started = time.monotonic()
    error: CheckError | None = None
    detail: str | None = None
    try:
        await provider.ping(settings.claude_model)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        error = "auth_failed"
    except anthropic.NotFoundError:
        error = "model_not_found"
    except anthropic.RateLimitError:
        error = "rate_limited"
    except anthropic.APIStatusError as e:
        error = "failed"
        detail = _redact(f"HTTP {e.status_code}: {e.message}"[:300], settings.claude_api_key)
    except anthropic.APIConnectionError:  # 含超时
        error = "unreachable"
    finally:
        await provider.aclose()
    if error is not None:
        return LLMCheckResult(ok=False, error=error, detail=detail)
    return LLMCheckResult(ok=True, latency_ms=max(1, int((time.monotonic() - started) * 1000)))
