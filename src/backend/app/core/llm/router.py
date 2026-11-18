"""按环节（stage）调用模型的路由器。

Polaris 只用 Claude：每个对话类环节都解析到同一个 provider、同一个模型
（``settings.claude_model``），key 与地址来自环境变量（见 core/config.py）。没有
服务商表、没有按环节选模型——数据库里的 LLMProviderConfig / ModelRoute / AcpAgent
不再参与任何模型调用。环节之间仍然不同的只有「耐心」和「思考深度」：

- 超时与重试次数：``call_profile``（短 / 中 / 长三档）；
- 思考深度（effort）与 ``max_tokens``：``stage_effort`` / ``stage_max_tokens``。

向量与重排是 Claude 没有的能力，走网关的 OpenAI 兼容端点（``settings.embedding_model``
/ ``settings.rerank_model``）；没配网关地址时这两个环节抛 NotImplementedError，由
调用方退回关键词检索。

每次 complete/stream 后写一条 LLMUsage 记账（拿不到 usage 时按 len/4 估算），
归属到 user + project + voyage；库侧调用另带 library_id。
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.llm import call_log
from app.core.llm.anthropic import (
    NONSTREAM_MAX_TOKENS,
    STREAM_MAX_TOKENS,
    AnthropicProvider,
    bare_model,
)
from app.core.llm.base import (
    CompletionResult,
    EffortLevel,
    JsonResult,
    LLMProvider,
    Message,
    StreamDone,
    StreamEvent,
    TextDelta,
)
from app.core.llm.budgets import InputBudget
from app.core.llm.errors import (
    LLMNotConfiguredError,
    LLMSchemaError,
    LLMTruncatedError,
    classify,
)
from app.core.llm.fake import FakeProvider, estimate_tokens
from app.core.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


#: ``complete_json`` 修复重问时最多把 ``max_tokens`` 放宽到多少（超过非流式上限走流式）。
JSON_REPAIR_MAX_TOKENS = STREAM_MAX_TOKENS
#: 校验失败重问时，回灌给模型的校验错误最多带多少字符。
_VALIDATION_FEEDBACK_CHARS = 2_000

# 科研环节枚举（docs/task-system.md §7；M2 新增 embedding，见 docs/task-system.md §7；
# 文献管理增强新增 reading（AI 伴读），见 docs/task-system.md §7（原 api-lit.md §3））
#
# librarian 与 extract 的分工：librarian = 长文本 + 多模态（wiki 图文精读编译、
# 论文图筛选注释、幻灯片大纲/内容/视觉评审），要强模型；extract = 纯文本短 JSON
# 结构化抽取（作者↔机构解析、概念定义批量生成、建库向导收录设置），小模型够用。
#
# 环节名会进 llm_usage / llm_call_logs 的 stage 列，前端按它显示用量与调用日志
# （``src/frontend/src/lib/stageLabels.ts`` 给每个名字配了标签）。所有环节用的是同一个
# Claude 模型，环节决定的只是耐心（call_profile）和思考深度（stage_effort）。
STAGES = (
    "default",
    "agent",
    "navigator",
    "sextant",
    "relevance",
    "librarian",
    "digest",
    "extract",
    "translation",
    "embedding",
    "rerank",
    "forge",
    "forge_generate",
    "forge_signal",
    "goal_explore",
    "proposal",
    "proposal_review",
    "debate",
    "discovery_plan",
    "experiment",
    "writing",
    "review",
    "reading",
    "citation_intent",
    # schema 引导的论文骨架抽取（#661）：整篇正文进、结构化 JSON 出
    "extract_skeleton",
    # 方法卡抽取（#663）：与骨架同形态（整篇正文进、短 JSON 出），单列环节是
    # 为了让管理员可以给方法卡配不同模型（它比骨架更吃「拆目的/机制」的判断力）
    "extract_method",
    # 缺口与负结果台账抽取（#665）：同为整篇正文进、条目列表 JSON 出
    "extract_gaps",
    # 库级 agentic RAG 三环节（#644）：扩展/重排是短 JSON，作答是中档长生成
    "rag_expand",
    "rag_rerank",
    "rag_answer",
    # 文献→假设四段管线（#648）：generate/ground 是较长的结构化生成（中档），
    # novelty/feasibility 是短 JSON 判定（短档）
    "hyp_generate",
    "hyp_ground",
    "hyp_novelty",
    "hyp_feasibility",
    # 假设锦标赛两两对比（#653）：短 JSON 判定（短档）
    "hyp_compare",
    # 假设写成想法草案（discovery.write_ideas）：一次一个假设，四段正文 + 评分
    "hyp_writeup",
    # 研究方向 → 检索简报（services/research_runs.py）：标题、英文关键词、分类、来源
    "research_brief",
)

# ---- 插件命名空间环节（#736，设计报告 §19） ----
#
# 上面的 STAGES 元组是内置集合，不随插件变动；学科包/插件要挂自己的 LLM 环节时
# 不再改核心表，而是在运行时注册一个三段式名字 ``plugin:<pack>:<stage>``。
# 段字符集限 [a-z0-9-]：环节名会进 model_routes / llm_usage / llm_call_logs 的
# stage 列和前端表格，放开大小写/下划线只会带来「同名不同串」的对账地狱。
# 总长限 32：三张表的 stage 列都是 String(32)（放宽列宽是另一条改造线的事，
# 这里先守住不写进截断串）。
#
# 与「没显式配路由就回退 default」的内置规则不同，插件环节多一级回退：注册时
# 可以声明一个**内置** fallback 环节（如 extract_skeleton），语义是「没单独配路由
# 时按这个内置环节的路由调用」。这不违背下面「界面说什么就是什么」的教训——
# 那条教训针对的是设置页画着「跟随默认」实际却走别处的行；插件环节在设置页
# 只有配了路由才出现（没有「跟随默认」的行画在那里骗人），fallback 是插件作者
# 显式声明、可在注册处查证的行为。fallback 只准指向内置环节：允许指向另一个
# 插件环节就会出现回退链，行为取决于插件加载顺序，没法讲清。
PLUGIN_STAGE_RE = re.compile(r"^plugin:([a-z0-9-]+):([a-z0-9-]+)$")
_PLUGIN_STAGE_MAX_LEN = 32  # ModelRoute/LLMUsage/LLMCallLog.stage 均为 String(32)


@dataclass(slots=True, frozen=True)
class PluginStageSpec:
    """一个已注册插件环节的运行时声明。tier 决定耐心档（见 call_profile），
    fallback 决定路由回退（见 resolve）。"""

    name: str  # 完整命名空间串 plugin:<pack>:<stage>
    tier: str  # "short" | "medium" | "long"
    fallback: str | None  # 内置环节名或 None（None = 直接回退 default）


_PLUGIN_STAGES: dict[str, PluginStageSpec] = {}


def is_plugin_stage(stage: str) -> bool:
    """名字是否为合法的插件命名空间串（只看形状，不看是否已注册）。"""
    return bool(PLUGIN_STAGE_RE.match(stage)) and len(stage) <= _PLUGIN_STAGE_MAX_LEN


def register_plugin_stage(
    pack: str, stage: str, *, tier: str = "medium", fallback: str | None = None
) -> str:
    """注册一个插件环节，返回完整命名空间串。

    重复注册：同名同配置幂等放过——抽取 schema 版本演进（同 id 覆盖注册）会把
    同一环节再挂一次，这不该炸；同名**不同配置**拒绝——两个插件抢同一个名字，
    或同一插件改了档位却没改版本，静默用先到的那份只会在线上表现成「配了没生效」。
    """
    name = f"plugin:{pack}:{stage}"
    if not PLUGIN_STAGE_RE.match(name):
        raise ValueError(
            f"invalid plugin stage name: {name!r} (pack/stage segments must match [a-z0-9-]+)"
        )
    if len(name) > _PLUGIN_STAGE_MAX_LEN:
        raise ValueError(
            f"plugin stage name too long: {name!r} "
            f"(max {_PLUGIN_STAGE_MAX_LEN} chars, the DB stage columns are String(32))"
        )
    if tier not in _TIER_PROFILES:
        raise ValueError(f"unknown tier: {tier!r} (expected one of {sorted(_TIER_PROFILES)})")
    if fallback is not None and fallback not in STAGES:
        raise ValueError(f"fallback must be a built-in stage, got {fallback!r}")
    spec = PluginStageSpec(name=name, tier=tier, fallback=fallback)
    existing = _PLUGIN_STAGES.get(name)
    if existing is not None:
        if existing == spec:
            return name
        raise ValueError(f"plugin stage {name!r} already registered with a different config")
    _PLUGIN_STAGES[name] = spec
    return name


def unregister_plugin_stage(name: str) -> bool:
    """注销一个插件环节（插件卸载/测试清场用）；返回是否真的删了。"""
    return _PLUGIN_STAGES.pop(name, None) is not None


def plugin_stage_spec(stage: str) -> PluginStageSpec | None:
    """已注册插件环节的声明；未注册返回 None。"""
    return _PLUGIN_STAGES.get(stage)


def known_stages() -> frozenset[str]:
    """当前进程认识的全部环节：内置 STAGES + 已注册的插件环节。"""
    return frozenset(STAGES) | _PLUGIN_STAGES.keys()


# 能力型环节：需要专用模型（嵌入/重排），Claude 不具备该能力，走网关的 OpenAI 兼容
# 端点。网关不可用时抛 NotImplementedError，由调用方降级（关键词检索等既有路径）。
_CAPABILITY_STAGES = frozenset({"embedding", "rerank"})

#: 各 Claude 模型的上下文窗口（token）。保守的常量表：认不出的模型按 20 万算——
#: 低估只是少放点材料，高估会让请求被服务端拒掉。
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-fable-5-2": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 200_000


def context_window_for(model: str) -> int:
    """模型的上下文窗口（token）。"""
    return _CONTEXT_WINDOWS.get(bare_model(model), _DEFAULT_CONTEXT_WINDOW)


@dataclass(slots=True, frozen=True)
class ResolvedRoute:
    provider_kind: str  # anthropic | openai_compat（仅向量/重排）| fake
    base_url: str | None
    api_key: str
    model: str
    #: 一律 None：当前 Claude 模型不接受采样参数。字段留着是因为调用方与测试替身
    #: 的签名里还有它。
    temperature: float | None = None
    provider_name: str = "fake"  # 调用日志里显示的名字
    effort: EffortLevel | None = None  # 该环节默认的思考深度
    #: 模型的上下文窗口（token）。助手的历史回放预算、各环节输入预算的封顶都靠它。
    context_window: int | None = None
    #: rerank 端点路径（相对 base_url）。None = provider 用自己的默认值。
    rerank_path: str | None = None


# 没有 key 且显式开了 fake 回退时的兜底：确定性 fake provider
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
#
# 换到 Claude 之后三档各放宽一倍：思考默认开着，十万 token 级的输入（整库摘要、
# 整篇正文）光是读完再想清楚就可能超过一分钟，60 秒的短档会把正常的调用掐掉重来。
# 短档仍然只试两次：卡死的请求不该被耐心地等上四轮。
_SHORT_CALL = (120.0, 2)  # (timeout 秒, 最多尝试次数) → 最坏 120+3+120 = 243s
_MEDIUM_CALL = (300.0, 2)
_LONG_CALL = (600.0, 4)

# 插件环节按注册时声明的 tier 取档（内置环节仍走下面三个集合的显式归类）
_TIER_PROFILES = {"short": _SHORT_CALL, "medium": _MEDIUM_CALL, "long": _LONG_CALL}

_SHORT_CALL_STAGES = frozenset(
    {
        "default",
        "sextant",
        "relevance",
        "extract",
        "translation",
        "embedding",
        "rerank",
        "forge",
        "forge_signal",
        "reading",
        "citation_intent",  # 引文意图批量分类：短 JSON、便宜模型（#639）
        "rag_expand",  # 库问答查询扩展：短 JSON（#644）
        "rag_rerank",  # 库问答重排+摘要：短 JSON（#644）
        "hyp_novelty",  # 假设查新逐条判定：短 JSON（#648）
        "hyp_feasibility",  # 假设可行性风险论证：短 JSON（#648）
        "hyp_compare",  # 假设锦标赛两两对比：短 JSON（#653）
        "research_brief",  # 一句方向 → 一份短的结构化简报
    }
)

# 「要给长耐心」与「要流式播出去」是两件事，不能共用一个集合。
# digest 就是反例：它输出 JSON（不该流式，否则整段 JSON 灌进任务终端日志），
# 但一次要为一批论文生成洞察，实测 p95 313 秒，必须给长档预算。
# forge_generate 同理：默认一次生成 8 个含方法、实验与风险的完整想法，生产请求
# 会超过 60 秒；仍保持非流式，避免把半截 JSON 写进任务终端。
_LONG_CALL_STAGES = STREAM_STAGES | frozenset({"digest", "forge_generate"})

# 中档用于多轮代理和较长的结构化生成。一次调用比短 JSON 长得多，但**不能**
# 直接塞进长档：那是 600s × 4 尝试，最坏 40 分钟攥着一个 HTTP 连接不放，而对话是同步的，
# 用户早走了。300s × 2 是"够想完一轮、又不至于把连接耗死"的折中。
# rag_answer 是同步问答里的长生成（证据集大、要逐句挂引用），短档会掐死它，
# 但它又是用户干等着的请求，不能给长档的耐心——与 agent 同档。
# hyp_generate / hyp_ground 同理：一次要组合多路灵感 / 拆解并逐条选证据，
# 输出比短 JSON 长得多，但 discovery 是逐候选串行调用，长档的耐心
# 会让一轮扩展的最坏时长完全失控。
_MEDIUM_CALL_STAGES = frozenset(
    {
        "agent",
        "discovery_plan",
        "goal_explore",
        "proposal_review",
        "rag_answer",
        "hyp_generate",
        "hyp_ground",
        "hyp_writeup",  # 假设写成想法：输入带原文片段、输出四段正文，与 hyp_generate 同档
        # 骨架抽取（#661）：一次吃约 2.4 万字正文、产四字段 JSON——比短 JSON 环节
        # 重得多（extract 短档喂的是单页级片段），但也没有长档的理由：
        # enrich 链逐篇串行，卡死一篇就堵住整条补全队列
        "extract_skeleton",
        "extract_method",  # 方法卡抽取（#663）：与骨架同一负载形态，同档
        # 缺口台账抽取（#665）：输入体量与骨架抽取相同（整篇正文），输出是最多
        # 8 条带原文摘录的条目列表——同样的「重于短档、不配长档」处境，同档
        "extract_gaps",
    }
)


def call_profile(stage: str) -> tuple[float, int]:
    """该环节单次调用的 (超时, 最大尝试次数)。

    三个集合都真的参与判定。曾经只判中/长两档、``else`` 一律短档，于是
    ``_SHORT_CALL_STAGES`` 成了只有测试在读的摆设——它和真实行为可以静默漂移，
    而测试还一直是绿的。现在漏归类的环节走 ``_SHORT_CALL`` 之前先喊一声：
    短档对一个长生成环节是致命的（#386 就是这么把想法生成掐死的），
    而这种事从日志里看只是「provider 失败」。
    """
    spec = _PLUGIN_STAGES.get(stage)
    if spec is not None:
        # 插件环节：档位在注册时声明（tier），不进上面三个内置集合——
        # 那三个集合与 STAGES 元组有守卫测试互相对账，混入运行时注册项会把守卫搅浑
        return _TIER_PROFILES[spec.tier]
    if stage in _MEDIUM_CALL_STAGES:
        return _MEDIUM_CALL
    if stage in _LONG_CALL_STAGES:
        return _LONG_CALL
    if stage not in _SHORT_CALL_STAGES:
        logger.warning(
            "stage %r has no call profile; defaulting to the short budget %s. "
            "Classify it in router.py, a long stage silently capped at that timeout looks "
            "like a provider outage.",
            stage,
            _SHORT_CALL,
        )
    return _SHORT_CALL


def _tier(stage: str) -> str:
    """环节所在的档位：short / medium / long（与 call_profile 同一套归类）。"""
    spec = _PLUGIN_STAGES.get(stage)
    if spec is not None:
        return spec.tier
    if stage in _MEDIUM_CALL_STAGES:
        return "medium"
    if stage in _LONG_CALL_STAGES:
        return "long"
    return "short"


#: 各档位默认的思考深度。短 JSON 环节（打分、判定、抽取）量大且简单，用最低档；
#: 多轮代理与较长的结构化生成用中档；长文本生成（编译、写作、评审）用高档。
#: 调用方显式传了 effort 就以它为准。
_TIER_EFFORT: dict[str, EffortLevel] = {"short": "low", "medium": "medium", "long": "high"}


def stage_effort(stage: str) -> EffortLevel:
    """该环节默认的思考深度。"""
    return _TIER_EFFORT[_tier(stage)]


def stage_max_tokens(stage: str, requested: int | None) -> int:
    """该环节这次调用的 ``max_tokens``。

    Claude 的 ``max_tokens`` 把思考和正文算在一起，所以调用方为「不思考的模型」定的
    小数字（比如一段短 JSON 给 300）不能照发，那会把回答截在半路。这里给每档一个
    下限：长文本环节和助手走流式，给足余量；其余环节非流式。调用方要得比下限多时
    听调用方的。
    """
    streams = _tier(stage) == "long" or stage == "agent"
    return max(requested or 0, STREAM_MAX_TOKENS if streams else NONSTREAM_MAX_TOKENS)


#: provider 客户端缓存的键：(kind, base_url, api_key, timeout, attempts, rerank_path)。
#: 与 ``_provider_for`` 构造键的地方保持一致。
_ProviderKey = dict[tuple[str, str | None, str, float, int, str | None], LLMProvider]


class LLMRouter:
    """stage → (provider 实例, model)；complete/stream 自动记账。

    ``event_bus`` 由驱动方（VoyageEngine）注入：置上后长文本 stage 的 complete()
    自动流式广播 token 增量到任务事件频道，对所有调用点透明。
    """

    def __init__(self) -> None:
        # 键含耐心档位（见 call_profile）：长/短两档各持一个客户端。键是实现细节，
        # 要在测试里替换 provider 请用 override_provider()，别直接往这个字典里塞。
        #
        # 凡是会改变客户端行为的字段都必须在键里，否则两份配置会共用同一个客户端。
        self._providers: _ProviderKey = {}
        self._override: LLMProvider | None = None
        self.event_bus: Any | None = None

    def override_provider(self, provider: LLMProvider | None) -> None:
        """强制所有环节都用这个 provider（测试注入点；传 None 取消）。"""
        self._override = provider

    def invalidate_cache(self) -> None:
        """丢掉缓存的 provider 客户端（配置来自环境变量，进程内不会变；测试改了
        settings 之后调用）。"""
        self._providers = {}

    def _chat_route(self, stage: str) -> ResolvedRoute | None:
        """对话环节的路由：有 key 就是 Claude，没有返回 None。"""
        settings = get_settings()
        if not settings.claude_configured:
            return None
        return ResolvedRoute(
            provider_kind="anthropic",
            base_url=settings.claude_base_url or None,
            api_key=settings.claude_api_key,
            model=settings.claude_model,
            provider_name="claude",
            effort=stage_effort(stage),
            context_window=context_window_for(settings.claude_model),
        )

    def _capability_route(self, stage: str) -> ResolvedRoute | None:
        """向量 / 重排的路由：有 key 且配了网关地址才有，否则 None。"""
        settings = get_settings()
        base = settings.gateway_openai_base_url
        if not settings.claude_configured or base is None:
            return None
        return ResolvedRoute(
            provider_kind="openai_compat",
            base_url=base,
            api_key=settings.claude_api_key,
            model=settings.embedding_model if stage == "embedding" else settings.rerank_model,
            provider_name="gateway",
            # LiteLLM 的重排开在 /v1/rerank（base 已含 /v1）
            rerank_path="/rerank",
        )

    def is_configured(self) -> bool:
        """有没有 Claude 的 key。

        问的是 key 本身，不看 ``llm_fake_fallback``——那个开关会让 resolve 在没有
        key 时也返回一个 fake provider，拿它当「配好了」的依据，等于告诉用户一件
        没发生的事。
        """
        return get_settings().claude_configured

    async def has_chat_model(self) -> bool:
        """能不能打到一个对话模型（= 有 Claude 的 key）。"""
        return self.is_configured()

    def _provider_for(self, route: ResolvedRoute, stage: str = "") -> LLMProvider:
        if self._override is not None:
            return self._override
        # 耐心程度进缓存键：同一份配置在长/短两档下各持一个客户端
        timeout, attempts = call_profile(stage)
        key = (
            route.provider_kind,
            route.base_url,
            route.api_key,
            timeout,
            attempts,
            # rerank 路径进键：它改的是请求地址
            route.rerank_path,
        )
        if key not in self._providers:
            if route.provider_kind == "anthropic":
                self._providers[key] = AnthropicProvider(
                    api_key=route.api_key,
                    base_url=route.base_url,
                    timeout=timeout,
                    max_attempts=attempts,
                    refusal_fallback=get_settings().claude_refusal_fallback,
                )
            elif route.provider_kind == "openai_compat":
                # 只用它的 embed / rerank（网关的 OpenAI 兼容端点）
                assert route.base_url is not None
                self._providers[key] = OpenAICompatProvider(
                    base_url=route.base_url,
                    api_key=route.api_key,
                    timeout=timeout,
                    max_attempts=attempts,
                    rerank_path=route.rerank_path,
                )
            elif route.provider_kind == "fake":
                self._providers[key] = FakeProvider()
            else:
                raise ValueError(f"unknown LLM provider kind: {route.provider_kind}")
        return self._providers[key]

    async def resolve(self, stage: str) -> tuple[LLMProvider, ResolvedRoute]:
        """该环节用哪个 provider、哪个模型。

        对话环节（含插件命名空间环节）一律是 Claude。没有 key 时抛
        LLMNotConfiguredError（503）；仅当显式开启 ``settings.llm_fake_fallback``
        （测试套件 / 无 key 演示）才回退 fake。

        能力型环节（``_CAPABILITY_STAGES``）走网关的向量 / 重排模型；网关不可用
        （没有 key，或没配网关地址）时抛 NotImplementedError，调用方按既有降级
        路径处理——同样只有显式开了 fake 回退且没有 key 时才给 fake。
        """
        if stage in _CAPABILITY_STAGES:
            route = self._capability_route(stage)
            if route is None:
                settings = get_settings()
                if settings.llm_fake_fallback and not settings.claude_configured:
                    route = _FALLBACK_ROUTE
                else:
                    raise NotImplementedError(
                        f"stage '{stage}' needs a gateway that serves embedding and rerank "
                        "models; none is configured"
                    )
        else:
            route = self._chat_route(stage)
            if route is None:
                if not get_settings().llm_fake_fallback:
                    raise LLMNotConfiguredError(
                        "no Claude API key: set ANTHROPIC_API_KEY (or LITELLM_API_KEY and "
                        "LITELLM_API_BASE for a LiteLLM gateway) and restart Polaris"
                    )
                route = _FALLBACK_ROUTE
        return self._provider_for(route, stage), route

    async def input_budget(self, spec: InputBudget) -> int:
        """该环节此刻生效的输入预算（字符），由 Claude 模型的上下文窗口推出，
        见 core/llm/budgets.py。没有 key（fake 回退）时用登记的默认值。"""
        route = self._chat_route(spec.stage)
        return spec.for_window(route.context_window if route is not None else None)

    async def model_name(self, stage: str) -> str | None:
        """该环节实际会用到的模型名；不可用时 None（调用方只用于展示）。

        ``embed()`` 只返回向量，模型名留在路由里；要把「这批向量是谁建的」记进库
        （papers.embedding_model 等）就得单独问一次。
        """
        try:
            _, route = await self.resolve(stage)
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
        from app.core.db import side_write
        from app.models.llm_config import LLMUsage

        row = LLMUsage(
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
            stage=stage,
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

        async def _write(session) -> None:
            session.add(row)

        # 记账尽力而为，失败不打断 LLM 主流程；调用方握着写锁时转后台写（见 side_write）
        await side_write(_write, what=f"llm usage accounting (stage={stage})")

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
        provider, route = await self.resolve(stage)
        temp = route.temperature if temperature is None else temperature
        eff = route.effort if effort is None else effort
        if route.provider_kind == "anthropic":
            max_tokens = stage_max_tokens(stage, max_tokens)
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

    # ---- 结构化输出 ----

    @staticmethod
    def _parse_json[SchemaT: BaseModel](schema: type[SchemaT], text: str) -> SchemaT:
        """把模型的回答校验成 schema 实例；不符合抛 ``ValidationError``。

        原生结构化输出回来的整段文本就是 JSON。没有原生支持的 provider（测试替身等）
        可能在 JSON 前后带说明文字，所以整段解析不了时再取最外层的花括号试一次。
        """
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start or (start == 0 and end == len(text) - 1):
                raise
            return schema.model_validate_json(text[start : end + 1])

    async def complete_json[SchemaT: BaseModel](
        self,
        stage: str,
        messages: Sequence[Message],
        *,
        schema: type[SchemaT],
        max_tokens: int | None = None,
        effort: EffortLevel | None = None,
        cache_prefix: str | None = None,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        library_id: uuid.UUID | None = None,
        voyage_id: uuid.UUID | None = None,
    ) -> JsonResult[SchemaT]:
        """要一个符合 ``schema``（Pydantic 模型）的回答，返回校验过的实例。

        建在 Claude 的结构化输出上（``output_config.format``）：服务端按 schema 约束
        解码，所以「回了一段不是 JSON 的文字」这类失败在结构上不会发生。schema 里
        接口不支持的约束（长度、数值范围）和自定义校验器，在这里用 Pydantic 再过一遍。

        一次逻辑调用**最多打两次模型**：

        - 输出被 ``max_tokens`` 截断：放宽上限重问一次（至少到默认值，封顶
          :data:`JSON_REPAIR_MAX_TOKENS`）；
        - 校验不通过：把校验错误附在后面重问一次；
        - 第二次还不行就抛 :class:`LLMTruncatedError` / :class:`LLMSchemaError`。
          拒答不重试。

        网络错误、429、5xx 的重试交给 SDK，这里不再套一层循环；SDK 重试用完仍然失败
        的，归类成 ``core/llm/errors.py`` 里带 ``transient`` 的错误再抛。

        ``max_tokens`` 是思考与正文合计的上限，默认 16,000。``cache_prefix`` 是多次
        调用共享的大段材料：放在第一条 user 消息之前并打缓存断点。记账与调用日志
        和 ``complete`` 同口径，每次模型调用各记一条。

        schema 只用封闭的对象：不要 ``dict[str, Any]``，不要递归；可选字段写
        ``X | None``。
        """
        provider, route = await self.resolve(stage)
        eff = route.effort if effort is None else effort
        limit = max_tokens or NONSTREAM_MAX_TOKENS
        convo: list[Message] = list(messages)
        total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        model = route.model
        failure: LLMSchemaError | LLMTruncatedError | None = None
        for call in (1, 2):
            result = await self._structured_call(
                stage,
                provider,
                route,
                convo,
                schema=schema,
                max_tokens=limit,
                effort=eff,
                cache_prefix=cache_prefix,
                user_id=user_id,
                project_id=project_id,
                library_id=library_id,
                voyage_id=voyage_id,
            )
            model = result.model or model
            for key, value in result.usage.items():
                total[key] = total.get(key, 0) + int(value)
            if result.finish_reason == "max_tokens":
                # 先看结束原因再碰正文：被截断的 JSON 是半截的，解析它没有意义
                failure = LLMTruncatedError(
                    f"{stage}: output hit max_tokens={limit} and was cut off"
                )
                wider = min(max(limit * 2, NONSTREAM_MAX_TOKENS), JSON_REPAIR_MAX_TOKENS)
                if wider <= limit:
                    break  # 已经在上限，再问一次也是同样的结果
                limit = wider
                continue
            try:
                data = self._parse_json(schema, result.content)
            except ValidationError as e:
                failure = LLMSchemaError(f"{stage}: output does not match {schema.__name__}: {e}")
                convo = list(messages)
                if result.content.strip():
                    convo.append(Message(role="assistant", content=result.content))
                convo.append(
                    Message(
                        role="user",
                        content=(
                            "Your previous reply did not pass validation:\n"
                            f"{str(e)[:_VALIDATION_FEEDBACK_CHARS]}\n"
                            "Reply again with a corrected JSON object that fixes these "
                            "problems. Keep everything that was already valid."
                        ),
                    )
                )
                continue
            return JsonResult(data=data, usage=total, model=model, calls=call)
        assert failure is not None
        raise failure

    async def _structured_call(
        self,
        stage: str,
        provider: LLMProvider,
        route: ResolvedRoute,
        messages: Sequence[Message],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        effort: EffortLevel | None,
        cache_prefix: str | None,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        library_id: uuid.UUID | None,
        voyage_id: uuid.UUID | None,
    ) -> CompletionResult:
        """一次结构化模型调用：记账、写调用日志、把 SDK 异常归类成 ``LLMError``。"""
        log_enabled = await call_log.logging_enabled()
        started_at = time.monotonic()
        identity: dict[str, Any] = {
            "user_id": user_id,
            "project_id": project_id,
            "voyage_id": voyage_id,
            "library_id": library_id,
        }
        logged: Sequence[Message] = (
            [Message(role="user", content=cache_prefix), *messages] if cache_prefix else messages
        )
        # 不是 LLMProvider 子类的测试替身没有这个方法：借基类的默认做法（走它的 complete）
        structured = getattr(provider, "complete_structured", None)
        kwargs: dict[str, Any] = {
            "model": route.model,
            "schema": schema,
            "max_tokens": max_tokens,
            "effort": effort,
            "cache_prefix": cache_prefix,
        }
        try:
            if structured is not None:
                result = await structured(messages, **kwargs)
            else:
                result = await LLMProvider.complete_structured(provider, messages, **kwargs)
        except Exception as e:
            if log_enabled:
                await self._log_call(
                    stage=stage,
                    route=route,
                    model=route.model,
                    started_at=started_at,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    request=call_log.sanitize_request(logged),
                    response=None,
                    usage={},
                    **identity,
                )
            typed = classify(e)
            if typed is None or typed is e:
                raise
            raise typed from e
        truncated = result.finish_reason == "max_tokens"
        result.usage = self._ensure_usage(logged, result.content, result.usage)
        await self._record_usage(stage=stage, model=result.model, usage=result.usage, **identity)
        if log_enabled:
            await self._log_call(
                stage=stage,
                route=route,
                model=result.model or route.model,
                started_at=started_at,
                status="error" if truncated else "ok",
                error="output hit max_tokens and was cut off" if truncated else None,
                request=call_log.sanitize_request(logged),
                response=result.content,
                usage=result.usage,
                **identity,
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
        import anthropic
        import httpx

        # 流开到一半断线：SDK 只在拿到响应之前自己重试，中途断了要这里整段重来
        transient = (httpx.TransportError, httpx.TimeoutException, anthropic.APIConnectionError)
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
            except transient as e:
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
        provider, route = await self.resolve(stage)
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
        provider, route = await self.resolve(stage)
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
        provider, route = await self.resolve(stage)
        if route.provider_kind == "anthropic":
            max_tokens = stage_max_tokens(stage, max_tokens)
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
        provider, route = await self.resolve(stage)
        if route.provider_kind == "anthropic":
            max_tokens = stage_max_tokens(stage, max_tokens)
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
