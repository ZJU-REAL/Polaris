"""Navigator（领航 · plan）：决定下一步怎么走。

它不调模型、不调工具、不碰数据库——因此整个类可以脱离 IO 单测。这条边界是刻意的：
「还能调几次工具」「这轮该不该硬关工具」「技能能不能收窄工具面」这些判断，出错的
后果（多烧一轮钱、或者提前收尾）都很贵，而它们本来就是纯逻辑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.core.llm.base import ToolResultBlock

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RoundPlan:
    """这一轮怎么跑。"""

    #: 这是最后一轮吗（到达 max_rounds）
    last_round: bool
    #: 本轮 token 预算已经用超了吗
    over_budget: bool

    @property
    def tool_choice(self) -> str | None:
        """到顶就硬关工具：让模型用已经拿到的东西收尾，而不是继续查。

        不学老 tool_loop 那样追加一条 user 消息哄模型收手——那是在请求它配合，
        而 tool_choice="none" 是让它没得选。
        """
        return "none" if (self.last_round or self.over_budget) else None


class Navigator:
    def __init__(self, *, max_rounds: int, max_turn_tokens: int | None = None) -> None:
        self._max_rounds = max_rounds
        self._max_turn_tokens = max_turn_tokens

    def plan_round(self, *, rounds_done: int, tokens_used: int) -> RoundPlan:
        return RoundPlan(
            last_round=rounds_done >= self._max_rounds,
            over_budget=(
                self._max_turn_tokens is not None and tokens_used >= self._max_turn_tokens
            ),
        )

    def stop_reason(self, plan: RoundPlan) -> str:
        if plan.over_budget:
            return "turn_budget"
        if plan.last_round:
            return "max_rounds"
        return "stop"

    def narrow_tools(
        self, results: list[ToolResultBlock], active: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """技能声明了 allowed-tools 就收窄后续工具面；没有可收窄的返回 None。

        **只能收窄，永不扩权**：技能里写一个本轮没给的工具名，结果是那个工具不可用，
        而不是把它加进来。空交集一律忽略——技能写错名字不该让 Buddy 变成哑巴。
        """
        for block in results:
            try:
                payload = json.loads(block.content)
            except ValueError:
                continue
            allowed = payload.get("allowed_tools") if isinstance(payload, dict) else None
            if not isinstance(allowed, list) or not allowed:
                continue
            narrowed = tuple(name for name in active if name in {str(a) for a in allowed})
            if narrowed:
                return narrowed
        return None
