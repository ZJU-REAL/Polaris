"""按环节配置的输入预算（#811）：一次调用最多往 prompt 里放多少字符的材料。

两个概念要分开：

- **上下文窗口**（``ModelRoute.context_window``，token）是模型能收多少——模型的属性；
- **输入预算**（``ModelRoute.input_budgets``，字符）是这个环节**打算**给多少——
  质量与成本之间的取舍。窗口有 1M 不等于每次精读都该塞 1M。

以前预算是散在各处的常量（``FULLTEXT_PROMPT_CHARS = 24000`` 之类），换了长窗口的
模型也用不上。现在每个可调的预算在这里登记一条：默认值就是原来的常量，所以不配置
的人行为完全不变；配置了的按上下限夹住，再被模型窗口封顶。

加一个新的可调预算：在下面登记一条 ``InputBudget``，调用处用 ``resolve_budget``
取值替换原来的常量即可——设置页的输入框、保存时的校验都跟着这张表走。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 窗口 token → 输入预算字符上限的换算：按「1 token ≈ 4 字符」估，输入最多占窗口
#: 一半，另一半留给系统提示、工具说明和输出。与对话历史回放预算同一口径
#: （api/chat_agent.py）。中文 1 字常接近 1 token，所以这是偏宽的上限，
#: 不是精确换算——它挡的是「窗口 8k 却配了 20 万字」这种一眼就错的配置。
CHARS_PER_WINDOW_TOKEN = 2


@dataclass(frozen=True, slots=True)
class InputBudget:
    """一个可调的输入预算。default 必须等于它替换掉的那个常量。"""

    stage: str
    key: str
    default: int
    minimum: int
    maximum: int

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
    stage="librarian", key="fulltext_chars", default=24_000, minimum=4_000, maximum=2_000_000
)
FORGE_CONTEXT = InputBudget(
    stage="forge", key="context_chars", default=12_000, minimum=2_000, maximum=2_000_000
)
FORGE_EXCERPT = InputBudget(
    stage="forge", key="excerpt_chars", default=800, minimum=200, maximum=50_000
)

INPUT_BUDGETS: tuple[InputBudget, ...] = (LIBRARIAN_FULLTEXT, FORGE_CONTEXT, FORGE_EXCERPT)


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


async def resolve_budget(llm: Any, spec: InputBudget, user_id: uuid.UUID | None) -> int:
    """从路由器取这个用户此刻生效的预算。

    测试替身和未接路由表的调用方没有 ``input_budget``：按默认值走，也就是改动前的
    行为——预算是可选的增强，拿不到时不能让编译/生成本身失败。
    """
    getter = getattr(llm, "input_budget", None)
    if getter is None:
        return spec.default
    return await getter(spec, user_id)
