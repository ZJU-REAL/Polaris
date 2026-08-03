"""子智能体：用自己的上下文干活，只把结论带回来。

这是 Claude Code 拿"宽度"而不付上下文代价的核心手法。差别在哪：

- 主 agent 自己扫 40 篇论文 → 40 条结果全部进它的历史，而且**此后每一轮都要重发**，
  几轮之后上下文就被搜索结果占满了，真正的对话反而被挤出去。
- 派个子 agent 去扫 → 那 40 条只存在于子 agent 的上下文里，用完即弃；父上下文里
  留下的是一段结论。

代价也要说清楚：子 agent 看不到父对话的历史（这正是它省上下文的原因），所以任务描述
必须自足——"接着刚才那个"这种话它读不懂。这条写进了工具描述里，让模型自己知道。
"""

import logging
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import tool

logger = logging.getLogger(__name__)

#: 子 agent 的轮次上限。比主循环短：它是来干一件具体的事的，不是来聊天的。
SUBAGENT_MAX_ROUNDS = 6

#: 子 agent 默认能用的工具。**不含 run_subagent 自己**——不允许再派子 agent，
#: 否则一个说不清的任务能递归炸开，而且父那边完全看不见发生了什么。
SUBAGENT_TOOLS: tuple[str, ...] = (
    "scan_papers",
    "grep_fulltext",
    "search_papers",
    "search_chunks",
    "get_paper",
    "read_fulltext",
    "read_wiki",
    "list_concepts",
    "get_concept",
)

_SYSTEM = """\
你是一个子任务执行者。你会拿到一件具体的事，用工具把它查清楚，然后给出结论。

要求：
- **你看不到主对话的历史**，只有下面这一条任务描述。不要假设任何未给出的上下文；
- 先用 scan_papers 铺开看有什么，再对少数几篇 read_fulltext 深读，不要一上来就深读；
- 结论要自足：把关键事实、论文标题与 id 都写进去，因为调用你的人只看得到这段结论；
- 查不到就说查不到，并说明你试过哪些查法。不要编。
"""


@tool(
    name="run_subagent",
    description=(
        "派一个子智能体去完成一件独立的、需要大量检索的任务，只把结论带回来。"
        "适用于会产生几十条中间结果的任务，例如梳理某个方向的相关工作、"
        "找出所有使用了某个数据集的论文。那些中间结果不会进入当前对话，只有结论会。"
        "注意：子智能体读不到当前对话的历史，任务描述必须自足。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要它做什么。写成完整的一句话，不要使用指代当前对话的说法",
            },
            "max_rounds": {
                "type": "integer",
                "description": f"最多几轮（默认 {SUBAGENT_MAX_ROUNDS}）",
            },
        },
        "required": ["task"],
    },
    summarize=lambda a, r: (
        f"子任务：{str(a.get('task') or '')[:40]}…"
        f"（{r.get('rounds', 0)} 轮、{r.get('tool_calls', 0)} 次工具）"
    ),
)
async def run_subagent(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    task = str(args.get("task") or "").strip()
    if not task:
        return {"error": "缺少 task"}
    max_rounds = max(1, min(int(args.get("max_rounds") or SUBAGENT_MAX_ROUNDS), 10))

    # 函数内 import：循环反过来要用工具注册表，模块级 import 会成环
    from app.agents.chat.events import DeltaEvent, DoneEvent, ErrorEvent, ToolResultEvent
    from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
    from app.core.llm.router import get_llm_router

    loop = ChatAgentLoop(llm=get_llm_router(), tool_ctx=ctx)
    req = ChatTurnRequest(
        conversation_id=ctx.project_id,  # 子 agent 不落库，这里只是个占位标识
        question=task,
        tool_names=SUBAGENT_TOOLS,
        max_rounds=max_rounds,
        extra_system=_SYSTEM,
    )

    parts: list[str] = []
    tool_calls = 0
    trace: list[str] = []
    stop_reason = "stop"
    error: str | None = None
    async for ev in loop.run(req):
        if isinstance(ev, DeltaEvent):
            parts.append(ev.text)
        elif isinstance(ev, ToolResultEvent):
            tool_calls += 1
            trace.append(f"{ev.name}：{ev.summary}")
        elif isinstance(ev, DoneEvent):
            stop_reason = ev.stop_reason
        elif isinstance(ev, ErrorEvent):
            error = ev.detail

    conclusion = "".join(parts).strip()
    if error and not conclusion:
        return {"error": error, "tool_calls": tool_calls}
    return {
        "conclusion": conclusion or "（子任务没有产出结论）",
        "rounds": min(max_rounds, tool_calls + 1),
        "tool_calls": tool_calls,
        # 只回**摘要**不回中间结果：把它们带回来就等于没派子 agent
        "trace": trace[:20],
        "stop_reason": stop_reason,
    }
