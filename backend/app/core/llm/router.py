"""按环节（stage）选择 provider/model 的路由器。

- 路由表存 DB（ModelRoute + LLMProviderConfig，管理端可改），60s 进程内缓存；
- 查不到路由时回退 settings 默认（FakeProvider，无 key 也能跑通）；
- 每次 complete/stream 后写一条 LLMUsage 记账（拿不到 usage 时按 len/4 估算），
  归属到 user + project + voyage。
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import CompletionResult, LLMProvider, Message
from app.core.llm.fake import FakeProvider, estimate_tokens
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.security import decrypt_secret

logger = logging.getLogger(__name__)

# 科研环节枚举（docs/api-m1.md §2；M2 新增 embedding，见 docs/api-m2.md §7；
# 文献管理增强新增 reading（AI 伴读），见 docs/api-lit.md §3）
STAGES = (
    "default",
    "navigator",
    "sextant",
    "interview",
    "relevance",
    "librarian",
    "embedding",
    "rerank",
    "forge",
    "forge_signal",
    "goal_explore",
    "proposal",
    "proposal_review",
    "debate",
    "experiment",
    "writing",
    "review",
    "reading",
)

_ROUTE_CACHE_TTL = 60.0


@dataclass(slots=True, frozen=True)
class ResolvedRoute:
    provider_kind: str  # openai_compat | anthropic | fake
    base_url: str | None
    api_key: str
    model: str
    temperature: float | None


# 无 DB 路由时的兜底：确定性 fake provider
_FALLBACK_ROUTE = ResolvedRoute(
    provider_kind="fake", base_url=None, api_key="", model="fake-default", temperature=0.0
)


# 长文本生成的 stage：complete() 有 event_bus + voyage_id 时改走流式并逐段广播
# llm_delta（任务详情页 terminal 实时展示"大模型正在输出什么"）。短 JSON 调用
# （relevance 打分 / sextant 判定等）不流式，避免噪声。
STREAM_STAGES = frozenset(
    {"navigator", "debate", "experiment", "writing", "proposal", "review", "librarian", "present"}
)
_STREAM_FLUSH_CHARS = 80  # token 增量攒到此长度再广播一段（节流，防刷爆 pub/sub）


class LLMRouter:
    """stage → (provider 实例, model)；complete/stream 自动记账。

    ``event_bus`` 由驱动方（VoyageEngine）注入：置上后长文本 stage 的 complete()
    自动流式广播 token 增量到任务事件频道，对所有调用点透明。
    """

    def __init__(self) -> None:
        self._routes: dict[str, ResolvedRoute] = {}
        self._routes_loaded_at: float = 0.0
        self._providers: dict[tuple[str, str | None, str], LLMProvider] = {}
        self.event_bus: Any | None = None

    def invalidate_cache(self) -> None:
        """管理端改动 providers/routes 后调用。"""
        self._routes_loaded_at = 0.0

    async def _load_routes(self) -> dict[str, ResolvedRoute]:
        from app.models.llm_config import LLMProviderConfig, ModelRoute

        routes: dict[str, ResolvedRoute] = {}
        async with get_sessionmaker()() as session:
            stmt = (
                select(ModelRoute, LLMProviderConfig)
                .join(LLMProviderConfig, ModelRoute.provider_id == LLMProviderConfig.id)
                .where(LLMProviderConfig.enabled.is_(True))
            )
            for route, provider in (await session.execute(stmt)).all():
                api_key = (
                    decrypt_secret(provider.api_key_encrypted) if provider.api_key_encrypted else ""
                )
                routes[route.stage] = ResolvedRoute(
                    provider_kind=provider.kind,
                    base_url=provider.base_url,
                    api_key=api_key,
                    model=route.model,
                    temperature=route.temperature,
                )
        return routes

    async def _get_routes(self) -> dict[str, ResolvedRoute]:
        now = time.monotonic()
        if now - self._routes_loaded_at > _ROUTE_CACHE_TTL:
            self._routes = await self._load_routes()
            self._routes_loaded_at = now
        return self._routes

    def _provider_for(self, route: ResolvedRoute) -> LLMProvider:
        key = (route.provider_kind, route.base_url, route.api_key)
        if key not in self._providers:
            if route.provider_kind == "openai_compat":
                from app.core.config import get_settings

                base_url = route.base_url or get_settings().openai_compat_base_url
                self._providers[key] = OpenAICompatProvider(
                    base_url=base_url, api_key=route.api_key
                )
            elif route.provider_kind == "anthropic":
                self._providers[key] = AnthropicProvider(api_key=route.api_key)
            elif route.provider_kind == "fake":
                self._providers[key] = FakeProvider()
            else:
                raise ValueError(f"unknown LLM provider kind: {route.provider_kind}")
        return self._providers[key]

    async def resolve(self, stage: str) -> tuple[LLMProvider, ResolvedRoute]:
        """先查 DB 路由表（缓存 60s），无则回退 default 路由，再回退 fake。"""
        routes = await self._get_routes()
        route = routes.get(stage) or routes.get("default") or _FALLBACK_ROUTE
        return self._provider_for(route), route

    async def _record_usage(
        self,
        *,
        stage: str,
        model: str,
        usage: dict[str, int],
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        voyage_id: uuid.UUID | None,
    ) -> None:
        from app.models.llm_config import LLMUsage

        try:
            async with get_sessionmaker()() as session:
                session.add(
                    LLMUsage(
                        user_id=user_id,
                        project_id=project_id,
                        voyage_id=voyage_id,
                        stage=stage,
                        model=model,
                        prompt_tokens=int(usage.get("prompt_tokens", 0)),
                        completion_tokens=int(usage.get("completion_tokens", 0)),
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — 记账尽力而为，失败不打断 LLM 主流程
            logger.warning("llm usage accounting failed (stage=%s)", stage, exc_info=True)

    @staticmethod
    def _ensure_usage(
        messages: Sequence[Message], content: str, usage: dict[str, int] | None
    ) -> dict[str, int]:
        """provider 未返回 usage 时按 len/4 估算。"""
        usage = dict(usage or {})
        if not usage.get("prompt_tokens"):
            usage["prompt_tokens"] = sum(estimate_tokens(m.content) for m in messages)
        if not usage.get("completion_tokens"):
            usage["completion_tokens"] = estimate_tokens(content)
        return usage

    async def complete(
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> CompletionResult:
        provider, route = await self.resolve(stage)
        temp = route.temperature if temperature is None else temperature
        # 长文本 stage 且有任务事件频道 + 非多模态：流式并逐段广播（终端实时展示）
        if (
            self.event_bus is not None
            and voyage_id is not None
            and stage in STREAM_STAGES
            and not images
        ):
            result = await self._stream_and_broadcast(
                stage, provider, route, messages, temp, max_tokens, voyage_id
            )
        else:
            # images 仅在提供时透传（兼容未声明该参数的 provider 子类/测试替身）
            extra: dict[str, Any] = {"images": images} if images else {}
            result = await provider.complete(
                messages, model=route.model, temperature=temp, max_tokens=max_tokens, **extra
            )
        result.usage = self._ensure_usage(messages, result.content, result.usage)
        await self._record_usage(
            stage=stage,
            model=result.model,
            usage=result.usage,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )
        return result

    async def _stream_and_broadcast(
        self,
        stage: str,
        provider: LLMProvider,
        route: ResolvedRoute,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int | None,
        voyage_id: uuid.UUID,
    ) -> CompletionResult:
        """流式补全并把 token 增量节流广播成 llm_delta 事件，返回拼好的完整结果。

        节流见 _STREAM_FLUSH_CHARS：攒够长度再发一段，避免每 token 刷爆 pub/sub；始终
        返回完整 content，对调用方与 complete() 等价（流式 provider 拿不到精确 usage，
        由 _ensure_usage 估算）。
        """
        collected: list[str] = []
        buf: list[str] = []
        buf_len = 0
        seq = 0

        async def flush() -> None:
            nonlocal buf, buf_len, seq
            if not buf:
                return
            await self.event_bus.publish_voyage_event(
                voyage_id, "llm_delta", {"stage": stage, "delta": "".join(buf), "seq": seq}
            )
            seq += 1
            buf, buf_len = [], 0

        await self.event_bus.publish_voyage_event(voyage_id, "llm_start", {"stage": stage})
        try:
            async for chunk in provider.stream(
                messages, model=route.model, temperature=temperature, max_tokens=max_tokens
            ):
                collected.append(chunk)
                buf.append(chunk)
                buf_len += len(chunk)
                if buf_len >= _STREAM_FLUSH_CHARS:
                    await flush()
            await flush()
        finally:
            await self.event_bus.publish_voyage_event(voyage_id, "llm_end", {"stage": stage})
        return CompletionResult(content="".join(collected), model=route.model)

    async def embed(
        self,
        texts: list[str],
        *,
        stage: str = "embedding",
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> list[list[float]]:
        """文本嵌入（stage 默认 embedding）。provider 不支持时抛 NotImplementedError。"""
        provider, route = await self.resolve(stage)
        vectors = await provider.embed(texts, model=route.model)
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage={"prompt_tokens": sum(estimate_tokens(t) for t in texts)},
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )
        return vectors

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        stage: str = "rerank",
        top_n: int | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> list[tuple[int, float]]:
        """重排（stage 默认 rerank），返回 (documents 下标, 分数) 降序。

        provider 不支持时抛 NotImplementedError；记账优先用响应的
        billed_units.total_tokens，拿不到则按 len/4 估算。
        """
        provider, route = await self.resolve(stage)
        result = await provider.rerank(query, documents, model=route.model, top_n=top_n)
        total_tokens = int(result.usage.get("total_tokens", 0)) or (
            estimate_tokens(query) + sum(estimate_tokens(d) for d in documents)
        )
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage={"prompt_tokens": total_tokens},
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )
        return result.results

    async def stream(
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> AsyncIterator[str]:
        provider, route = await self.resolve(stage)
        collected: list[str] = []
        async for chunk in provider.stream(
            messages,
            model=route.model,
            temperature=route.temperature if temperature is None else temperature,
            max_tokens=max_tokens,
        ):
            collected.append(chunk)
            yield chunk
        content = "".join(collected)
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage=self._ensure_usage(messages, content, None),
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
        )


_router: LLMRouter | None = None


def get_llm_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def reset_llm_router() -> None:
    """测试用：丢弃单例（清空缓存与 provider 实例）。"""
    global _router
    _router = None
