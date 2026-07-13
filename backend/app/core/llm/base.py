"""LLM 抽象层。

业务代码只允许依赖本模块的 ``LLMProvider`` 接口（complete/stream），
不允许直接 import openai/anthropic SDK。具体实现见 openai_compat.py / anthropic.py。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

Role = str  # "system" | "user" | "assistant"


@dataclass(slots=True)
class Message:
    role: Role
    content: str


@dataclass(slots=True)
class CompletionResult:
    content: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens/completion_tokens/...


class LLMProvider(ABC):
    """异步 LLM Provider 接口。"""

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """一次性补全，返回完整结果。"""

    @abstractmethod
    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式补全，逐段 yield 文本增量（用于 SSE 转发）。"""
