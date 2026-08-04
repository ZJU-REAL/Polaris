"""任务计划工具：让 Buddy 把多步任务的打算说出来，并在推进时更新。

这是本轮唯一一个"写"了什么的工具，但它**什么平台数据都没动**——计划只活在这场
对话里（回喂给模型 + 推给前端画进度）。所以它照样 ``read_only=True``：只读的语义
是"不改平台状态"，不是"不返回状态"。真要动库里的东西还得等写工具那一期。

为什么值得单独做一个工具，而不是让模型在正文里写「我打算先……再……」：正文里的
计划**不可寻址**。模型写完就漂在文字里，下一轮它自己也未必记得刚才排的顺序，前端
更没法把"第 2 步做完了"画出来。给它一个结构化的落点，计划才既能被更新，也能被看见。
"""

from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import tool

#: 计划最多几步。多于这个数说明任务该拆，不该在一条计划里排下去。
MAX_STEPS = 12

#: 步骤状态。三档就够：没做、在做、做完。
STATUSES = ("pending", "running", "done")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "这一步要做什么，一句话"},
                    "status": {
                        "type": "string",
                        "enum": list(STATUSES),
                        "description": "pending 未开始 / running 进行中 / done 已完成",
                    },
                },
                "required": ["title"],
            },
            "description": "完整的计划。每次都传全量，不是增量补丁",
        }
    },
    "required": ["steps"],
}


class PlanError(ValueError):
    """计划本身不合法（空计划、步骤没有标题、同时有多步在跑）。"""


def normalize(steps: Any) -> list[dict[str, str]]:
    """把模型给的东西整成规范的计划；不合法就抛，由循环转成错误结果回喂。

    宽进严出：状态拼错、字段多了都容忍；但**空计划**和**没标题的步骤**必须报错，
    否则前端会画出一个空白进度条，比不画更糟。
    """
    if not isinstance(steps, list) or not steps:
        raise PlanError("steps 不能为空：计划至少要有一步")
    if len(steps) > MAX_STEPS:
        raise PlanError(f"计划最多 {MAX_STEPS} 步，超了就说明任务该拆")
    out: list[dict[str, str]] = []
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise PlanError(f"第 {i} 步不是对象")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise PlanError(f"第 {i} 步没有 title")
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in STATUSES:
            status = "pending"
        out.append({"title": title[:200], "status": status})
    running = [s for s in out if s["status"] == "running"]
    if len(running) > 1:
        raise PlanError("同时只能有一步是 running——并行推进的计划没人看得懂")
    return out


@tool(
    "update_plan",
    description=(
        "把多步任务的计划写下来，并在推进时更新每步状态。"
        "任务需要三步以上、或者用户要看你打算怎么做时调用。"
        "每次传完整计划（不是增量），开始做某步就把它标成 running，做完标 done。"
        "一步就能答完的问题不要调用它。"
    ),
    input_schema=_SCHEMA,
    summarize=lambda args, result: (
        f"计划 {len(result.get('steps', []))} 步"
        f"（{sum(1 for s in result.get('steps', []) if s['status'] == 'done')} 步已完成）"
    ),
)
async def update_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    steps = normalize(args.get("steps"))
    return {"steps": steps}
