"""模型调用的类型化错误。

``LLMRouter.complete_json`` 只抛这里的错误（以及调用方自己的编程错误）。每个错误带
三样东西：

- ``code``：稳定的机器码，前端按它给出文案（``lib/errors.ts``）；
- ``transient``：过一会儿重试有没有可能成功。限流、过载、网络中断是；密钥不对、
  请求本身非法、模型拒答、输出被截断、结构不符合要求都不是——这些需要人来处理，
  自动重试只是把同样的失败再花一遍钱；
- ``user_message``：能直接给用户看的一句话，不含堆栈和内部术语。

在此之前这些失败长得一样：服务端回了 429 或一段报错文本，调用方照样拿去当 JSON
解析，三次之后报「LLM 连续输出非法 JSON」（#826）。
"""

import asyncio
from typing import Any

import anthropic
import httpx


class LLMError(RuntimeError):
    """模型调用失败。子类只定默认值，判断一律看 ``code`` / ``transient``。"""

    code: str = "LLM_ERROR"
    transient: bool = False
    user_message: str = "模型调用失败，请稍后重试"

    def __init__(
        self,
        detail: str | None = None,
        *,
        user_message: str | None = None,
        transient: bool | None = None,
    ) -> None:
        if user_message is not None:
            self.user_message = user_message
        if transient is not None:
            self.transient = transient
        super().__init__(detail or self.user_message)

    def as_dict(self) -> dict[str, Any]:
        """落进 checkpoint / 事件里的形状。"""
        return {
            "code": self.code,
            "transient": self.transient,
            "message": self.user_message,
            "detail": str(self),
        }


class LLMNotConfiguredError(LLMError):
    """没有 Claude 的 API key（且未开启 fake 回退）。

    main.py 的异常处理器把它映射为 503 LLM_NOT_CONFIGURED，前端据此告诉用户
    怎么提供 key（设置环境变量后重新打开 Polaris）。
    """

    code = "LLM_NOT_CONFIGURED"
    user_message = "还没有配置模型服务的密钥"


class LLMTransportError(LLMError):
    """连不上模型服务，或等到超时（SDK 自己的重试已经用完）。"""

    code = "LLM_UNREACHABLE"
    transient = True
    user_message = "无法连接模型服务，请稍后重试"


class LLMRateLimitedError(LLMError):
    """429：请求太频繁。"""

    code = "LLM_RATE_LIMITED"
    transient = True
    user_message = "模型服务请求过于频繁，请稍后重试"


class LLMOverloadedError(LLMError):
    """5xx / 529：模型服务暂时不可用。"""

    code = "LLM_OVERLOADED"
    transient = True
    user_message = "模型服务暂时繁忙，请稍后重试"


class LLMAuthError(LLMError):
    """401 / 403：密钥无效或没有权限。"""

    code = "LLM_AUTH"
    user_message = "模型服务拒绝了请求，请在设置中检查密钥"


class LLMRequestError(LLMError):
    """其余 4xx：请求本身不被接受（参数、模型名、内容过长等），重试同一个请求没有用。"""

    code = "LLM_BAD_REQUEST"
    user_message = "模型服务不接受这次请求"


class LLMTruncatedError(LLMError):
    """输出撞到 ``max_tokens`` 被截断，放宽一次之后仍然不够。"""

    code = "LLM_TRUNCATED"
    user_message = "模型的回答太长，没有写完"


class LLMRefusalError(LLMError):
    """Claude 拒绝了这次请求（``stop_reason == "refusal"``）。

    不是网络或配置问题，重试同一个请求没有用。``category`` 是分类器给的类别
    （可能为空）。
    """

    code = "LLM_REFUSED"
    user_message = "模型拒绝了这次请求"

    def __init__(self, category: str | None = None, explanation: str | None = None) -> None:
        self.category = category
        self.explanation = explanation
        detail = f" ({category})" if category else ""
        super().__init__(f"Claude declined this request{detail}.")


class LLMSchemaError(LLMError):
    """模型的输出不符合要求的结构，带着校验错误重问一次之后仍然不符合。"""

    code = "LLM_SCHEMA"
    user_message = "模型的回答不符合要求的格式"


def _status_detail(exc: anthropic.APIStatusError) -> str:
    kind = getattr(exc, "type", None)
    suffix = f" {kind}" if kind else ""
    return f"HTTP {exc.status_code}{suffix}: {exc.message}"


def classify(exc: BaseException) -> LLMError | None:
    """把 SDK / 传输层的异常归到上面的类型；认不出的返回 None（原样上抛）。

    顺序从具体到一般。``APITimeoutError`` 是 ``APIConnectionError`` 的子类，
    两者都算传输失败。
    """
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMTransportError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimitedError(_status_detail(exc))
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMAuthError(_status_detail(exc))
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or getattr(exc, "type", None) == "overloaded_error":
            return LLMOverloadedError(_status_detail(exc))
        if exc.status_code == 408:
            return LLMTransportError(_status_detail(exc))
        return LLMRequestError(_status_detail(exc))
    if isinstance(exc, anthropic.APIError):
        # 流到一半服务端发来的 error 事件：没有 HTTP 状态，按类型名判断
        if getattr(exc, "type", None) in ("overloaded_error", "api_error", "timeout_error"):
            return LLMOverloadedError(f"{type(exc).__name__}: {exc}")
        return None
    if isinstance(exc, httpx.TransportError | asyncio.TimeoutError | ConnectionError):
        return LLMTransportError(f"{type(exc).__name__}: {exc}")
    try:  # SDK 2.x 建在 httpx2 上；流到一半断线时抛的是它的异常
        import httpx2
    except ImportError:  # pragma: no cover — 旧 SDK 没有这个依赖
        return None
    if isinstance(exc, httpx2.TransportError):
        return LLMTransportError(f"{type(exc).__name__}: {exc}")
    return None


__all__ = [
    "LLMAuthError",
    "LLMError",
    "LLMNotConfiguredError",
    "LLMOverloadedError",
    "LLMRateLimitedError",
    "LLMRefusalError",
    "LLMRequestError",
    "LLMSchemaError",
    "LLMTransportError",
    "LLMTruncatedError",
    "classify",
]
