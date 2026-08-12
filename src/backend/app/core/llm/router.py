"""按环节（stage）选择 provider/model 的路由器。

- 路由表存 DB（ModelRoute + LLMProviderConfig，管理端可改），60s 进程内缓存；
- 查不到路由时回退 settings 默认（FakeProvider，无 key 也能跑通）；
- 每次 complete/stream 后写一条 LLMUsage 记账（拿不到 usage 时按 len/4 估算），
  归属到 user + project + voyage；库侧调用（ingest/打分/编译/概念定义/向量化）
  另带 library_id 记到方向库账上（P6 库级月度预算的依据）。
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.llm import call_log
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import (
    CompletionResult,
    EffortLevel,
    LLMProvider,
    Message,
    StreamDone,
    StreamEvent,
    TextDelta,
)
from app.core.llm.fake import FakeProvider, estimate_tokens
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.security import decrypt_secret

logger = logging.getLogger(__name__)


class LLMNotConfiguredError(RuntimeError):
    """没有配置任何可用的 LLM 路由（且未开启 fake 回退）。

    main.py 的异常处理器把它映射为 503 LLM_NOT_CONFIGURED，
    前端据此提示「请先在设置里配置大模型」。
    """

# 科研环节枚举（docs/api-m1.md §2；M2 新增 embedding，见 docs/api-m2.md §7；
# 文献管理增强新增 reading（AI 伴读），见 docs/api-lit.md §3）
#
# librarian 与 extract 的分工：librarian = 长文本 + 多模态（wiki 图文精读编译、
# 论文图筛选注释、幻灯片大纲/内容/视觉评审），要强模型；extract = 纯文本短 JSON
# 结构化抽取（作者↔机构解析、概念定义批量生成、建库向导收录设置），小模型够用。
#
# **这个元组必须和前端 ``src/frontend/src/lib/api.ts`` 的 ``LLM_STAGES`` 逐项对齐。**
# 两边漂了就是双向坏：这里少一个，设置页照样把它画出来，管理员一配，整表覆盖的
# PUT 被 400 `unknown stage` 顶回来——不是那一行被忽略，是**整张路由表存不进去**；
# 这里多一个，那个环节在界面上根本不存在，只能改数据库。digest 少了一年多没人发现，
# 因为它当时偷偷继承 librarian，看起来「能用」（见下面的回退说明）。
STAGES = (
    "default",
    "agent",
    "navigator",
    "sextant",
    "relevance",
    "librarian",
    "digest",
    "extract",
    "embedding",
    "rerank",
    "forge",
    "forge_generate",
    "forge_signal",
    "goal_explore",
    "proposal",
    "proposal_review",
    "debate",
    "experiment",
    "writing",
    "review",
    "reading",
    "feedback_issue",
)

_ROUTE_CACHE_TTL = 60.0

# 能力型环节：需要专用模型（嵌入/重排），对话模型不具备该能力，
# 因此不回退 default 路由——未配置时抛 NotImplementedError，由调用方降级
# （关键词检索/向量分兜底等既有路径）。
_CAPABILITY_STAGES = frozenset({"embedding", "rerank"})

# 只走全局配置的环节：忽略调用方传的 user_id，一律用 owner=NULL 的路由。
# 嵌入在此——论文/分段/想法的向量是全平台共享的一份数据，每个自管用户各用各的
# 模型建向量，池子里就会混进互不可比的坐标系；更要命的是查询向量也会跟着变，
# 维度恰好相同时不报任何错，只是排序全乱。所以嵌入模型不给用户自选。
# rerank 不在此列：它是逐条打分，不涉及跨调用可比性，各用各的没有问题。
GLOBAL_ONLY_STAGES = frozenset({"embedding"})

# 没显式配路由的环节一律回退 default——设置页就是这么写的（「未单独设置的环节自动
# 跟随默认」），那一行还直接显示着 default 的模型名。
#
# 这里曾经有一张兼容表：digest 拆出来时回退 librarian、agent 回退 reading，理由是
# 「存量部署不配也不该改变行为」。代价是设置页从此在说谎——管理员把默认设成 A、
# 界面上「每日研究简报」显示跟随默认 A，实际调用打的却是 B。2026-08-10 生产就栽在
# 这上面：默认配的是 qwen-flash，简报却一直走 librarian 的 gpt-5.6-luna，那天网关
# 抖动返回截断 JSON，三个库的同步全停了，排查时谁也想不到它没走默认。
#
# 「行为不变」保的是没人看的存量默认值，「界面说什么就是什么」保的是每一次人工配置。
# 后者更值钱：想要原来的模型，显式配一行就是了，而界面骗人是查不出来的。


@dataclass(slots=True, frozen=True)
class ResolvedRoute:
    provider_kind: str  # openai_compat | anthropic | fake
    base_url: str | None
    api_key: str
    model: str
    temperature: float | None
    provider_name: str = "fake"  # 管理端 provider 名称（调用日志用）
    user_agent: str | None = None  # Provider 级客户端标识；None = HTTP 客户端默认值
    effort: EffortLevel | None = None  # 推理档位，None = 不发送该参数（用模型默认）
    #: 模型的上下文窗口（token）。None = 管理端没填，调用方按保守常量走。
    #: agent 的历史回放预算靠它——不知道窗口多大，裁剪阈值就只能拍脑袋。
    context_window: int | None = None


# 无 DB 路由时的兜底：确定性 fake provider
_FALLBACK_ROUTE = ResolvedRoute(
    provider_kind="fake",
    base_url=None,
    api_key="",
    model="fake-default",
    temperature=0.0,
    provider_name="fake",
    effort=None,
)


# 长文本生成的 stage：complete() 有 event_bus + voyage_id 时改走流式并逐段广播
# llm_delta（任务详情页 terminal 实时展示"大模型正在输出什么"）。短 JSON 调用
# （relevance 打分 / sextant 判定 / extract 结构化抽取等）不流式，避免噪声——
# 流式路径还会把整段输出写进 voyage_terminal_logs，JSON 混进终端毫无可读性。
STREAM_STAGES = frozenset(
    {"navigator", "debate", "experiment", "writing", "proposal", "review", "librarian"}
)
_STREAM_FLUSH_CHARS = 80  # token 增量攒到此长度再广播一段（节流，防刷爆 pub/sub）
_STREAM_RETRY_ATTEMPTS = 3  # 流式请求网络瞬断整段重试次数
_STREAM_RETRY_BASE_SECONDS = 2.0

# 一次调用能等多久、重试几次，按环节分三档。
#
# 生产实测（gpt-5.6-luna，6 小时）：relevance 中位 13.3s，但 p95 达到 1215s。
# 那不是模型在慢慢想——4 次尝试 × 300s 超时 + 退避 3+6+12 正好是 1221s，也就是
# 重试循环耐心地熬过四次各 5 分钟的超时。而打分协程全程攥着一个数据库连接，
# 于是连接池被卡死的协程占满，其余论文打分批量超时、状态停在 candidate。
#
# 短 JSON 环节（打分 / 判定 / 结构化抽取）中位数都在十几秒，没有理由等 300 秒，
# 更没理由等四次；卡住的短请求重试也极少能救回来。长文本环节（编译 / 写作 /
# 评审）本来就要跑几分钟，保持宽松。
_SHORT_CALL = (60.0, 2)  # (timeout 秒, 最多尝试次数) → 最坏 60+3+60 = 123s
_MEDIUM_CALL = (180.0, 2)
_LONG_CALL = (300.0, 4)

_SHORT_CALL_STAGES = frozenset(
    {
        "default",
        "sextant",
        "relevance",
        "extract",
        "embedding",
        "rerank",
        "forge",
        "forge_signal",
        "reading",
        "feedback_issue",
    }
)

# 「要给长耐心」与「要流式播出去」是两件事，不能共用一个集合。
# digest 就是反例：它输出 JSON（不该流式，否则整段 JSON 灌进任务终端日志），
# 但一次要为一批论文生成洞察，实测 p95 313 秒，必须给长档预算。
# forge_generate 同理：默认一次生成 8 个含方法、实验与风险的完整想法，生产请求
# 会超过 60 秒；仍保持非流式，避免把半截 JSON 写进任务终端。
_LONG_CALL_STAGES = STREAM_STAGES | frozenset({"digest", "forge_generate"})

# 中档用于多轮代理和较长的结构化生成。一次调用比短 JSON 长得多，但**不能**
# 直接塞进长档：那是 300s × 4 尝试，最坏 20 分钟攥着一个 HTTP 连接不放，而对话是同步的，
# 用户早走了。180s × 2 是"够想完一轮、又不至于把连接耗死"的折中。
_MEDIUM_CALL_STAGES = frozenset({"agent", "goal_explore", "proposal_review"})


def call_profile(stage: str) -> tuple[float, int]:
    """该环节单次调用的 (超时, 最大尝试次数)。

    三个集合都真的参与判定。曾经只判中/长两档、``else`` 一律短档，于是
    ``_SHORT_CALL_STAGES`` 成了只有测试在读的摆设——它和真实行为可以静默漂移，
    而测试还一直是绿的。现在漏归类的环节走 ``_SHORT_CALL`` 之前先喊一声：
    短档 60s 对一个长生成环节是致命的（#386 就是这么把想法生成掐死的），
    而这种事从日志里看只是「provider 失败」。
    """
    if stage in _MEDIUM_CALL_STAGES:
        return _MEDIUM_CALL
    if stage in _LONG_CALL_STAGES:
        return _LONG_CALL
    if stage not in _SHORT_CALL_STAGES:
        logger.warning(
            "stage %r has no call profile; defaulting to the short budget %s. "
            "Classify it in router.py, a long stage silently capped at 60s looks "
            "like a provider outage.",
            stage,
            _SHORT_CALL,
        )
    return _SHORT_CALL


class LLMRouter:
    """stage → (provider 实例, model)；complete/stream 自动记账。

    ``event_bus`` 由驱动方（VoyageEngine）注入：置上后长文本 stage 的 complete()
    自动流式广播 token 增量到任务事件频道，对所有调用点透明。
    """

    def __init__(self) -> None:
        # 路由表按 owner 分别缓存：None=全局(admin)，<user>=该用户自管
        self._routes_by_owner: dict[uuid.UUID | None, dict[str, ResolvedRoute]] = {}
        self._routes_loaded_at: dict[uuid.UUID | None, float] = {}
        # 用户 llm_self_managed 标志缓存（(flag, loaded_at)）
        self._self_managed: dict[uuid.UUID, tuple[bool, float]] = {}
        # 键含耐心档位（见 call_profile）：长/短两档各持一个客户端。键是实现细节，
        # 要在测试里替换 provider 请用 override_provider()，别直接往这个字典里塞。
        self._providers: dict[tuple[str, str | None, str, float, int], LLMProvider] = {}
        self._override: LLMProvider | None = None
        self.event_bus: Any | None = None

    def override_provider(self, provider: LLMProvider | None) -> None:
        """强制所有环节都用这个 provider（测试注入点；传 None 取消）。

        以前测试是直接往 ``_providers`` 里按键塞，于是缓存键的形状变成了事实上的
        公开接口——给它加个「耐心档位」维度就一次打断 16 个用例。走这个口子，
        键怎么变都与测试无关。
        """
        self._override = provider

    def invalidate_cache(self) -> None:
        """管理端 / 用户改动 providers/routes/接管状态后调用（清所有 owner）。"""
        self._routes_loaded_at.clear()
        self._self_managed.clear()

    async def _load_routes(self, owner_id: uuid.UUID | None) -> dict[str, ResolvedRoute]:
        from app.models.llm_config import LLMProviderConfig, ModelRoute

        routes: dict[str, ResolvedRoute] = {}
        async with get_sessionmaker()() as session:
            stmt = (
                select(ModelRoute, LLMProviderConfig)
                .join(LLMProviderConfig, ModelRoute.provider_id == LLMProviderConfig.id)
                .where(
                    LLMProviderConfig.enabled.is_(True),
                    ModelRoute.owner_id == owner_id
                    if owner_id is not None
                    else ModelRoute.owner_id.is_(None),
                )
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
                    provider_name=provider.name,
                    user_agent=provider.user_agent,
                    effort=route.effort,
                    context_window=route.context_window,
                )
        return routes

    async def _get_routes(self, owner_id: uuid.UUID | None) -> dict[str, ResolvedRoute]:
        now = time.monotonic()
        if now - self._routes_loaded_at.get(owner_id, 0.0) > _ROUTE_CACHE_TTL:
            self._routes_by_owner[owner_id] = await self._load_routes(owner_id)
            self._routes_loaded_at[owner_id] = now
        return self._routes_by_owner.get(owner_id, {})

    async def _is_self_managed(self, user_id: uuid.UUID) -> bool:
        now = time.monotonic()
        cached = self._self_managed.get(user_id)
        if cached is not None and now - cached[1] < _ROUTE_CACHE_TTL:
            return cached[0]
        from app.models.user import User

        async with get_sessionmaker()() as session:
            user = await session.get(User, user_id)
            flag = bool(user and user.llm_self_managed)
        self._self_managed[user_id] = (flag, now)
        return flag

    async def _effective_owner(
        self, user_id: uuid.UUID | None, stage: str | None = None
    ) -> uuid.UUID | None:
        """自管用户 → 用自己的配置；否则（含无 user_id 的系统调用）→ 全局(admin)。

        ``GLOBAL_ONLY_STAGES`` 里的环节例外：无条件用全局配置。
        """
        if user_id is None or stage in GLOBAL_ONLY_STAGES:
            return None
        return user_id if await self._is_self_managed(user_id) else None

    def _provider_for(self, route: ResolvedRoute, stage: str = "") -> LLMProvider:
        if self._override is not None:
            return self._override
        # 耐心程度进缓存键：同一个 provider 配置在长/短两档下各持一个客户端
        timeout, attempts = call_profile(stage)
        key = (
            route.provider_kind,
            route.base_url,
            route.api_key,
            route.user_agent,
            timeout,
            attempts,
        )
        if key not in self._providers:
            if route.provider_kind == "openai_compat":
                base_url = route.base_url or get_settings().openai_compat_base_url
                self._providers[key] = OpenAICompatProvider(
                    base_url=base_url,
                    api_key=route.api_key,
                    timeout=timeout,
                    max_attempts=attempts,
                )
            elif route.provider_kind == "anthropic":
                self._providers[key] = AnthropicProvider(
                    api_key=route.api_key,
                    base_url=route.base_url,
                    user_agent=route.user_agent,
                    timeout=timeout,
                )
            elif route.provider_kind == "fake":
                self._providers[key] = FakeProvider()
            else:
                raise ValueError(f"unknown LLM provider kind: {route.provider_kind}")
        return self._providers[key]

    async def resolve(
        self, stage: str, user_id: uuid.UUID | None = None
    ) -> tuple[LLMProvider, ResolvedRoute]:
        """按有效 owner 查路由表（缓存 60s），无则回退 default 路由。

        没显式配的环节一律回退 ``default``，不存在「继承另一个环节」这回事——设置页
        就是这么告诉管理员的，界面显示跟随默认哪个模型，实际就得打那个模型。

        owner 由 user 的接管状态决定：自管用户用自己的 owner=user 配置（admin 的
        对他失效——即"配好前不可用"）；被接管用户及无 user_id 的系统调用用
        全局(owner=NULL, admin)配置。``GLOBAL_ONLY_STAGES``（嵌入）不吃这一套，
        无论谁调都用全局配置。

        能力型环节（``_CAPABILITY_STAGES``）不回退 default：对话模型没有
        embedding/rerank 能力，回退只会产生无意义调用；未显式配置时抛
        NotImplementedError，调用方按既有降级路径处理。

        对话环节连 default 也没有时抛 LLMNotConfiguredError（503），不再
        静默回退演示用 fake provider；仅当显式开启
        ``settings.llm_fake_fallback``（测试套件 / 无 key 演示）才回退 fake。
        """
        owner_id = await self._effective_owner(user_id, stage)
        routes = await self._get_routes(owner_id)
        route = routes.get(stage)
        if route is None:
            if stage in _CAPABILITY_STAGES:
                if not routes and get_settings().llm_fake_fallback:
                    route = _FALLBACK_ROUTE
                else:
                    raise NotImplementedError(f"no route configured for stage '{stage}'")
            else:
                route = routes.get("default")
                if route is None:
                    if not get_settings().llm_fake_fallback:
                        raise LLMNotConfiguredError(
                            "no LLM provider configured — add a provider and routes in settings"
                        )
                    route = _FALLBACK_ROUTE
        return self._provider_for(route, stage), route

    async def model_name(self, stage: str, user_id: uuid.UUID | None = None) -> str | None:
        """该环节实际会用到的模型名；未配置/不可用时 None（调用方只用于展示）。

        ``embed()`` 只返回向量，模型名留在路由里；要把「这批向量是谁建的」记进库
        （papers.embedding_model 等）就得单独问一次。resolve 有 60s 路由缓存，
        额外开销可忽略。
        """
        try:
            _, route = await self.resolve(stage, user_id)
        except (NotImplementedError, LLMNotConfiguredError):
            return None
        return route.model

    async def _record_usage(
        self,
        *,
        stage: str,
        model: str,
        usage: dict[str, int],
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        voyage_id: uuid.UUID | None,
        library_id: uuid.UUID | None = None,
    ) -> None:
        from app.models.llm_config import LLMUsage

        try:
            async with get_sessionmaker()() as session:
                session.add(
                    LLMUsage(
                        user_id=user_id,
                        project_id=project_id,
                        library_id=library_id,
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

    async def _log_call(
        self,
        *,
        stage: str,
        route: ResolvedRoute,
        model: str,
        started_at: float,
        status: str,
        error: str | None,
        request: Any | None,
        response: str | None,
        usage: dict[str, int],
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        voyage_id: uuid.UUID | None,
        library_id: uuid.UUID | None = None,
    ) -> None:
        """写一条调用日志（开关打开时才被调用）。任何失败只 warning，不影响主流程。"""
        try:
            await call_log.record_call(
                stage=stage,
                provider_name=route.provider_name,
                model=model,
                # monotonic 计时；快到 1ms 内的调用向上取整，保证时延恒为正
                duration_ms=max(1, int((time.monotonic() - started_at) * 1000)),
                status=status,
                error=error,
                request=request,
                response=response,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
            )
        except Exception:  # noqa: BLE001 — 日志记录绝不影响业务调用
            logger.warning("llm call logging failed (stage=%s)", stage, exc_info=True)

    @staticmethod
    def _ensure_usage(
        messages: Sequence[Message], content: str, usage: dict[str, int] | None
    ) -> dict[str, int]:
        """provider 未返回 usage 时按 len/4 估算。"""
        usage = dict(usage or {})
        if not usage.get("prompt_tokens"):
            usage["prompt_tokens"] = sum(estimate_tokens(m.text) for m in messages)
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
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> CompletionResult:
        provider, route = await self.resolve(stage, user_id)
        temp = route.temperature if temperature is None else temperature
        eff = route.effort if effort is None else effort
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        try:
            # 长文本 stage 且有任务事件频道：流式并逐段广播（终端实时展示"大模型正在输出什么"）。
            # 多模态（带图，如 wiki 精读编译）也走流式——图片附在请求上、正文照常逐段吐。
            if self.event_bus is not None and voyage_id is not None and stage in STREAM_STAGES:
                result = await self._stream_and_broadcast(
                    stage, provider, route, messages, temp, max_tokens, voyage_id, images, eff
                )
            else:
                # images/effort 仅在提供时透传（兼容未声明这些参数的 provider 子类/测试替身）
                extra: dict[str, Any] = {"images": images} if images else {}
                if eff is not None:
                    extra["effort"] = eff
                if tools:
                    # 只在真的要用工具时才传：未声明该参数的 provider 子类/测试替身
                    # 一旦收到未知关键字就会 TypeError，而它们本来工作得好好的
                    extra["tools"] = tools
                    if tool_choice is not None:
                        extra["tool_choice"] = tool_choice
                result = await provider.complete(
                    messages, model=route.model, temperature=temp, max_tokens=max_tokens, **extra
                )
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=call_log.sanitize_request(messages, images),
                    response=None,
                    usage={},
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                    library_id=library_id,
                )
            raise
        result.usage = self._ensure_usage(messages, result.content, result.usage)
        await self._record_usage(
            stage=stage,
            model=result.model,
            usage=result.usage,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
            library_id=library_id,
        )
        if log_enabled:
            await self._log_call(
                stage=stage,
                route=route,
                model=result.model or route.model,
                started_at=started_at,
                status="ok",
                error=None,
                request=call_log.sanitize_request(messages, images),
                response=result.content,
                usage=result.usage,
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
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
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> CompletionResult:
        """流式补全并把 token 增量节流广播成 llm_delta 事件，返回拼好的完整结果。

        节流见 _STREAM_FLUSH_CHARS：攒够长度再发一段，避免每 token 刷爆 pub/sub；始终
        返回完整 content，对调用方与 complete() 等价（流式 provider 拿不到精确 usage，
        由 _ensure_usage 估算）。

        网络瞬断重试：非流式路径的 _post_with_retry 会自动重试 TransportError，流式
        路径以前没有——LLM 网关一次 ReadTimeout 就把整个任务步骤打失败（线上实测，
        冒烟/分析步接连因此转向用户提问）。这里整段重来：每次重试重新广播 llm_start
        （前端会另起一个输出块），退避后拉新流，收齐才算成功。
        """
        import httpx

        stream_extra: dict[str, Any] = {"effort": effort} if effort is not None else {}
        collected: list[str] = []
        last_exc: Exception | None = None
        for attempt in range(_STREAM_RETRY_ATTEMPTS):
            collected = []
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
                    messages,
                    model=route.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    images=images,
                    **stream_extra,
                ):
                    collected.append(chunk)
                    buf.append(chunk)
                    buf_len += len(chunk)
                    if buf_len >= _STREAM_FLUSH_CHARS:
                        await flush()
                await flush()
            except (httpx.TransportError, httpx.TimeoutException) as e:
                await self.event_bus.publish_voyage_event(voyage_id, "llm_end", {"stage": stage})
                last_exc = e
                if attempt >= _STREAM_RETRY_ATTEMPTS - 1:
                    raise
                delay = _STREAM_RETRY_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "LLM 流式请求网络错误（%s），%.0fs 后整段重试（%d/%d）",
                    type(e).__name__,
                    delay,
                    attempt + 1,
                    _STREAM_RETRY_ATTEMPTS,
                )
                # 终端可见（线上实测：重试只在 worker 日志里，用户盯着终端静默十几分钟）
                await self.event_bus.publish_voyage_event(
                    voyage_id,
                    "log",
                    {
                        "message": f"大模型网络请求中断（{type(e).__name__}），"
                        f"{delay:.0f} 秒后自动重试（{attempt + 1}/{_STREAM_RETRY_ATTEMPTS}）",
                        "level": "info",
                    },
                )
                await asyncio.sleep(delay)
                continue
            except BaseException:
                await self.event_bus.publish_voyage_event(voyage_id, "llm_end", {"stage": stage})
                raise
            await self.event_bus.publish_voyage_event(voyage_id, "llm_end", {"stage": stage})
            last_exc = None
            break
        if last_exc is not None:  # 理论上到不了（最后一次失败已 raise），防御性兜底
            raise last_exc
        full_text = "".join(collected)
        # 大模型完整输出落库，供刷新后 / 事后回放（实时增量已通过 llm_delta 走 SSE）。
        if full_text:
            from app.services.voyage_logs import record_terminal_log

            await record_terminal_log(voyage_id, "llm", message=full_text, stage=stage)
        return CompletionResult(content=full_text, model=route.model)

    async def embed(
        self,
        texts: list[str],
        *,
        stage: str = "embedding",
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> list[list[float]]:
        """文本嵌入（stage 默认 embedding）。provider 不支持时抛 NotImplementedError。"""
        provider, route = await self.resolve(stage, user_id)
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        # 调用日志只记摘要（输入条数 + 首条截断），不存向量
        request_summary = {
            "texts_count": len(texts),
            "first_text": call_log.truncate_text(texts[0], call_log.SUMMARY_MAX_CHARS)
            if texts
            else "",
        }
        try:
            vectors = await provider.embed(texts, model=route.model)
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=request_summary,
                    response=None,
                    usage={},
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                    library_id=library_id,
                )
            raise
        usage = {"prompt_tokens": sum(estimate_tokens(t) for t in texts)}
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage=usage,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
            library_id=library_id,
        )
        if log_enabled:
            dim = len(vectors[0]) if vectors else 0
            await self._log_call(
                stage=stage,
                route=route,
                model=route.model,
                started_at=started_at,
                status="ok",
                error=None,
                request=request_summary,
                response=f"[{len(vectors)} embeddings, dim={dim}]",
                usage=usage,
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
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
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> list[tuple[int, float]]:
        """重排（stage 默认 rerank），返回 (documents 下标, 分数) 降序。

        provider 不支持时抛 NotImplementedError；记账优先用响应的
        billed_units.total_tokens，拿不到则按 len/4 估算。
        """
        provider, route = await self.resolve(stage, user_id)
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        # 调用日志只记摘要（query + 文档条数 + 首条截断），不存全部文档
        request_summary = {
            "query": call_log.truncate_text(query, call_log.SUMMARY_MAX_CHARS),
            "documents_count": len(documents),
            "first_document": call_log.truncate_text(documents[0], call_log.SUMMARY_MAX_CHARS)
            if documents
            else "",
        }
        try:
            result = await provider.rerank(query, documents, model=route.model, top_n=top_n)
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=request_summary,
                    response=None,
                    usage={},
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                    library_id=library_id,
                )
            raise
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
            library_id=library_id,
        )
        if log_enabled:
            await self._log_call(
                stage=stage,
                route=route,
                model=route.model,
                started_at=started_at,
                status="ok",
                error=None,
                request=request_summary,
                response=f"[{len(result.results)} rerank results]",
                usage={"prompt_tokens": total_tokens},
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
            )
        return result.results

    async def stream(
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        effort: EffortLevel | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> AsyncIterator[str]:
        provider, route = await self.resolve(stage, user_id)
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        collected: list[str] = []
        eff = route.effort if effort is None else effort
        extra: dict[str, Any] = {"effort": eff} if eff is not None else {}
        try:
            async for chunk in provider.stream(
                messages,
                model=route.model,
                temperature=route.temperature if temperature is None else temperature,
                max_tokens=max_tokens,
                **extra,
            ):
                collected.append(chunk)
                yield chunk
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=call_log.sanitize_request(messages),
                    response="".join(collected) or None,  # 已收到的部分输出
                    usage={},
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                    library_id=library_id,
                )
            raise
        content = "".join(collected)
        usage = self._ensure_usage(messages, content, None)
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage=usage,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
            library_id=library_id,
        )
        # 时延 = 到流结束的完整耗时；response 聚合完整输出
        if log_enabled:
            await self._log_call(
                stage=stage,
                route=route,
                model=route.model,
                started_at=started_at,
                status="ok",
                error=None,
                request=call_log.sanitize_request(messages),
                response=content,
                usage=usage,
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
            )


    async def stream_events(
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """结构化流式：文本 / 思考 / 工具调用。agent 循环用它，普通对话仍走 stream()。

        记账与 stream() 同口径：流正常结束后写一行 LLMUsage。provider 给了 usage 就用
        它的（Anthropic 现在也归一化过键名了），没给才按 len/4 估。
        """
        provider, route = await self.resolve(stage, user_id)
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        collected: list[str] = []
        reported: dict[str, int] = {}
        eff = route.effort if effort is None else effort
        extra: dict[str, Any] = {}
        if eff is not None:
            extra["effort"] = eff
        if images:
            extra["images"] = images
        if tools:
            extra["tools"] = tools
            if tool_choice is not None:
                extra["tool_choice"] = tool_choice
        try:
            async for ev in provider.stream_events(
                messages,
                model=route.model,
                temperature=route.temperature if temperature is None else temperature,
                max_tokens=max_tokens,
                **extra,
            ):
                if isinstance(ev, TextDelta):
                    collected.append(ev.text)
                elif isinstance(ev, StreamDone) and ev.usage:
                    reported = dict(ev.usage)
                yield ev
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=call_log.sanitize_request(messages),
                    response="".join(collected) or None,
                    usage={},
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                    library_id=library_id,
                )
            raise
        content = "".join(collected)
        usage = reported or self._ensure_usage(messages, content, None)
        await self._record_usage(
            stage=stage,
            model=route.model,
            usage=usage,
            user_id=user_id,
            project_id=project_id,
            voyage_id=voyage_id,
            library_id=library_id,
        )
        if log_enabled:
            await self._log_call(
                stage=stage,
                route=route,
                model=route.model,
                started_at=started_at,
                status="ok",
                error=None,
                request=call_log.sanitize_request(messages),
                response=content,
                usage=usage,
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
                library_id=library_id,
            )


_router: LLMRouter | None = None


def get_llm_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def reset_llm_router() -> None:
    """测试用：丢弃单例（清空缓存与 provider 实例），并重置调用日志开关缓存。"""
    global _router
    _router = None
    call_log.reset_state()
