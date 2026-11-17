"""各环节的输入预算：一次调用最多往 prompt 里放多少字符的材料。

两个概念要分开：

- **上下文窗口**（token）是模型能收多少——模型的属性，见 router.context_window_for；
- **输入预算**（字符）是这个环节**打算**给多少——质量与成本之间的取舍。窗口有 1M
  不等于每次精读都该塞 1M。

预算由模型的窗口推出，不再有按环节的人工配置：窗口够大（Claude Opus 5 是 1M）时
用 ``large_window`` 那一档，一篇论文的正文可以整篇放进去；窗口小或不知道时用
``default``（改造前散在各处的常量）。两档都再被窗口封顶。

加一个新的预算：在下面登记一条 ``InputBudget``，调用处用 ``resolve_budget`` 取值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 窗口 token → 输入预算字符上限的换算：按「1 token ≈ 4 字符」估，输入最多占窗口
#: 一半，另一半留给系统提示、工具说明和输出。与对话历史回放预算同一口径
#: （api/chat_agent.py）。中文 1 字常接近 1 token，所以这是偏宽的上限，
#: 不是精确换算——它挡的是「窗口 8k 却配了 20 万字」这种一眼就错的配置。
CHARS_PER_WINDOW_TOKEN = 2

#: 窗口到这个大小就算「大窗口」，各预算改用 large_window 那一档。
LARGE_WINDOW_TOKENS = 500_000


@dataclass(frozen=True, slots=True)
class InputBudget:
    """一个可调的输入预算。default 必须等于它替换掉的那个常量。"""

    stage: str
    key: str
    default: int
    minimum: int
    maximum: int

    #: 窗口不小于 LARGE_WINDOW_TOKENS 时用的值；None = 与 default 相同
    large_window: int | None = None

    def for_window(self, context_window: int | None) -> int:
        """按模型窗口取预算：大窗口用 large_window，否则 default；再被窗口封顶。"""
        large = context_window is not None and context_window >= LARGE_WINDOW_TOKENS
        value = self.large_window if large and self.large_window is not None else self.default
        return self.effective(value, context_window)

    def effective(self, configured: int | None, context_window: int | None) -> int:
        """实际生效的字符数：配置值（缺省用默认）夹进上下限，再按模型窗口封顶。

        窗口封顶排在最后且可以压到下限以下：窗口真的那么小时，塞更多只会让请求
        被服务端拒掉或截断，下限在这里没有意义。
        """
        value = self.default if configured is None else configured
        value = min(max(value, self.minimum), self.maximum)
        cap = window_cap(context_window)
        return value if cap is None else min(value, cap)


def window_cap(context_window: int | None) -> int | None:
    """模型窗口允许的输入字符上限；窗口没填时 None（不封顶）。"""
    return context_window * CHARS_PER_WINDOW_TOKEN if context_window else None


# ---- 登记表 ----

LIBRARIAN_FULLTEXT = InputBudget(
    stage="librarian",
    key="fulltext_chars",
    default=24_000,
    minimum=4_000,
    maximum=2_000_000,
    # 约 4 万 token：绝大多数论文的正文可以整篇进去，不再只看开头两万多字
    large_window=160_000,
)
FORGE_CONTEXT = InputBudget(
    stage="forge",
    key="context_chars",
    default=12_000,
    minimum=2_000,
    maximum=2_000_000,
    large_window=48_000,
)
FORGE_EXCERPT = InputBudget(
    stage="forge", key="excerpt_chars", default=800, minimum=200, maximum=50_000, large_window=2_000
)

INPUT_BUDGETS: tuple[InputBudget, ...] = (LIBRARIAN_FULLTEXT, FORGE_CONTEXT, FORGE_EXCERPT)


# ---- 固定上限（不随配置变）----
#
# 「一次调用给模型看多少材料」的常量集中放在这里，调用处只引用名字。它们是按 Claude
# 的窗口定的：1M token 的上下文里，十几条 1,500 字的证据片段只是零头，而片段太短
# （原来是 300 字）往往带不全它要佐证的那句论断。成本型的旋钮（每次检索多少篇、
# 最多跑几轮）不在这里，在各自的请求 schema 里。

# 假设探索（agents/voyage/actions_discovery.py、services/hypothesis_pipeline.py）
#: 没指定时跑几轮扩展。每轮约「子假设数 × 4」次模型调用；上限在 schemas/voyage.py。
DISCOVERY_DEFAULT_EXPANSIONS = 4
#: 每轮扩展最多产出几个子假设。
DISCOVERY_CHILDREN_PER_ROUND = 4
#: 一个假设最多拆成几条子命题。
HYPOTHESIS_MAX_SUBCLAIMS = 6
#: 每条子命题给模型挑选的检索片段数（接地与查新共用）。
HYPOTHESIS_EVIDENCE_PER_SUBCLAIM = 12
#: 引文远端 / 多样窗两路灵感各取几条。
HYPOTHESIS_INSPIRATIONS_PER_ROUTE = 8
#: 方法类比 / 未连接概念对 / 开放缺口三路燃料各取几条。
HYPOTHESIS_FUELS_PER_ROUTE = 8
#: 灵感与证据片段的截断长度（字符）。
HYPOTHESIS_SNIPPET_CHARS = 1_500

# 库内检索（services/library_rag.py）
#: 每轮检索取回的片段数。假设生成的语义近邻一路直接用它。
RAG_TOP_K = 24
#: 送去重排的候选上限（重排是一次模型调用，输入要有上界）。
RAG_RERANK_POOL = 80


def budgets_for(stage: str) -> dict[str, InputBudget]:
    return {b.key: b for b in INPUT_BUDGETS if b.stage == stage}


def validate_budgets(
    stage: str, budgets: Mapping[str, int] | None, context_window: int | None
) -> None:
    """保存路由前的校验；不合法抛 ValueError，信息直接给设置页显示。

    认不出的键一律拒收，不静默丢弃：丢了的话，用户以为配上了，实际什么都没发生。
    """
    if not budgets:
        return
    known = budgets_for(stage)
    cap = window_cap(context_window)
    for key, value in budgets.items():
        spec = known.get(key)
        if spec is None:
            raise ValueError(f"stage '{stage}' has no input budget named '{key}'")
        if not spec.minimum <= value <= spec.maximum:
            raise ValueError(
                f"{stage}.{key} must be between {spec.minimum} and {spec.maximum} characters"
            )
        if cap is not None and value > cap:
            raise ValueError(
                f"{stage}.{key} = {value} characters does not fit a {context_window}-token "
                f"context window (at most {cap})"
            )


async def resolve_budget(llm: Any, spec: InputBudget) -> int:
    """从路由器取此刻生效的预算。

    测试替身和未接路由表的调用方没有 ``input_budget``：按默认值走，也就是改动前的
    行为——预算是可选的增强，拿不到时不能让编译/生成本身失败。
    """
    getter = getattr(llm, "input_budget", None)
    if getter is None:
        return spec.default
    return await getter(spec)
