"""按环节（stage）选择 provider/model 的路由器骨架。

TODO(M2): 路由表落 DB（管理端可配置），此处的 DEFAULT_ROUTES 仅作初始回退。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import CompletionResult, LLMProvider, Message
from app.core.llm.openai_compat import OpenAICompatProvider


@dataclass(slots=True, frozen=True)
class RouteTarget:
    provider: str  # "openai_compat" | "anthropic"
    model: str


# 环节 → 模型的初始映射（占位值，待管理端/DB 配置覆盖）
DEFAULT_ROUTES: dict[str, RouteTarget] = {
    "interview": RouteTarget("anthropic", "claude-sonnet-4-5"),
    "survey": RouteTarget("openai_compat", "deepseek-chat"),
    "scoring": RouteTarget("openai_compat", "deepseek-chat"),
    "ideation": RouteTarget("anthropic", "claude-sonnet-4-5"),
    "review": RouteTarget("anthropic", "claude-sonnet-4-5"),
    "coding": RouteTarget("anthropic", "claude-sonnet-4-5"),
    "writing": RouteTarget("anthropic", "claude-sonnet-4-5"),
    "default": RouteTarget("openai_compat", "deepseek-chat"),
}


class LLMRouter:
    """按 stage 解析出 (provider, model)，并提供 complete 便捷入口。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._providers: dict[str, LLMProvider] = {}
        self._routes = dict(DEFAULT_ROUTES)

    def _get_provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            if name == "openai_compat":
                self._providers[name] = OpenAICompatProvider(
                    base_url=self._settings.openai_compat_base_url,
                    api_key=self._settings.openai_compat_api_key,
                )
            elif name == "anthropic":
                self._providers[name] = AnthropicProvider(
                    api_key=self._settings.anthropic_api_key,
                )
            else:
                raise ValueError(f"unknown LLM provider: {name}")
        return self._providers[name]

    def resolve(self, stage: str) -> tuple[LLMProvider, str]:
        """按环节返回 (provider 实例, model 名)。TODO(M2): 先查 DB 路由表。"""
        target = self._routes.get(stage) or self._routes["default"]
        return self._get_provider(target.provider), target.model

    async def complete(
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        provider, model = self.resolve(stage)
        return await provider.complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
