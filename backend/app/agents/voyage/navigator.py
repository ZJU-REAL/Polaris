"""Navigator（领航 · planning）：把目标分解为步骤列表；执行失败时依据诊断重规划。

- 输出严格 JSON（{"steps": [...]}），做 schema 校验，解析失败重试 2 次；
- ``demo`` kind 用固定三步计划（不依赖 LLM 规划质量），第 2 步声明
  ``requires_gate="compute_budget"`` 以演示人在环闸门。
"""

import json
from typing import Any

from app.agents.voyage.actions import known_actions
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun

_MAX_ATTEMPTS = 3  # 首次 + 重试 2 次

PLAN_SYSTEM_PROMPT = """\
你是 Navigator，负责把科研目标分解为可执行的步骤计划。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"steps": [{"title": "步骤标题", "action": "动作名", "params": {}, \
"acceptance": "验收标准", "requires_gate": null}]}
约束：
- action 只能取：%(actions)s
- llm.complete 的 params 需含 stage 与 prompt（prompt 可用 {goal} 模板变量）
- artifact.write 的 params 需含 name 与 content
- 需要人工审批的步骤把 requires_gate 设为闸门类型（如 "compute_budget"），否则为 null
- 步骤控制在 5 个以内
"""

REPLAN_SYSTEM_PROMPT = """\
你是 Navigator，负责在某个步骤验证失败后重新规划剩余步骤。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"steps": [{"title": "步骤标题", "action": "动作名", "params": {}, \
"acceptance": "验收标准", "requires_gate": null}]}
约束与初次规划相同；action 只能取：%(actions)s。
输出的 steps 将替换原计划中失败步骤起的剩余部分。
"""


class NavigatorError(Exception):
    """规划失败（LLM 连续产出非法 JSON 等）。"""


def demo_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """demo 航程固定三步：分析目标 → 生成产物（过闸门）→ 总结。"""
    return [
        {
            "title": "分析目标",
            "action": "llm.complete",
            "params": {
                "stage": "navigator",
                "prompt": "请分析以下科研目标，列出关键问题与切入点：{goal}",
            },
            "acceptance": "输出包含对目标的分析要点",
            "requires_gate": None,
        },
        {
            "title": "生成产物",
            "action": "artifact.write",
            "params": {
                "name": "demo-report.md",
                "content": "# Demo 航程产物\n\n目标：{goal}\n\n（由 Voyage demo 航程生成）\n",
            },
            "acceptance": "产物已写入 checkpoint",
            "requires_gate": "compute_budget",
        },
        {
            "title": "总结",
            "action": "llm.complete",
            "params": {
                "stage": "navigator",
                "prompt": "请对围绕以下目标的本次航程做一个简短总结：{goal}",
            },
            "acceptance": "输出包含总结内容",
            "requires_gate": None,
        },
    ]


def _extract_json(content: str) -> Any:
    """截取首个 '{' 到末个 '}' 之间的内容解析（容忍代码块围栏/前后杂讯）。"""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


def validate_steps(data: Any) -> list[dict[str, Any]]:
    """校验 {"steps": [...]} schema，返回规范化步骤列表；非法则抛 ValueError。"""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError('expected {"steps": [...]}')
    steps: list[dict[str, Any]] = []
    actions = known_actions()
    for i, raw in enumerate(data["steps"]):
        if not isinstance(raw, dict):
            raise ValueError(f"step {i} is not an object")
        title = raw.get("title")
        action = raw.get("action")
        params = raw.get("params") or {}
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"step {i} missing title")
        if action not in actions:
            raise ValueError(f"step {i} has unknown action: {action!r}")
        if not isinstance(params, dict):
            raise ValueError(f"step {i} params is not an object")
        requires_gate = raw.get("requires_gate")
        if requires_gate is not None and not isinstance(requires_gate, str):
            raise ValueError(f"step {i} requires_gate must be string or null")
        acceptance = raw.get("acceptance")
        if acceptance is not None and not isinstance(acceptance, str):
            raise ValueError(f"step {i} acceptance must be string or null")
        steps.append(
            {
                "title": title.strip(),
                "action": action,
                "params": params,
                "acceptance": acceptance,
                "requires_gate": requires_gate or None,
            }
        )
    if not steps:
        raise ValueError("plan has no steps")
    return steps


class Navigator:
    def __init__(self, llm: LLMRouter) -> None:
        self._llm = llm

    async def _ask_for_steps(
        self, run: VoyageRun, system: str, user_prompt: str
    ) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for _attempt in range(_MAX_ATTEMPTS):
            result = await self._llm.complete(
                "navigator",
                [
                    Message(role="system", content=system),
                    Message(role="user", content=user_prompt),
                ],
                user_id=run.created_by,
                project_id=run.project_id,
                voyage_id=run.id,
            )
            try:
                return validate_steps(_extract_json(result.content))
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
        raise NavigatorError(f"navigator produced invalid plan: {last_error}")

    async def plan(self, run: VoyageRun, context: dict[str, Any] | None = None) -> list[dict]:
        """目标 → 步骤列表。demo kind 走固定计划，其余 kind 用 LLM 规划。"""
        if run.kind == "demo":
            return demo_plan(run)
        system = PLAN_SYSTEM_PROMPT % {"actions": ", ".join(sorted(known_actions()))}
        user_prompt = f"目标：{run.goal}"
        if context:
            user_prompt += f"\n上下文：{json.dumps(context, ensure_ascii=False, default=str)}"
        return await self._ask_for_steps(run, system, user_prompt)

    async def replan(
        self, run: VoyageRun, failed_step: dict[str, Any], diagnosis: str
    ) -> list[dict[str, Any]]:
        """验证失败后重规划：返回替换「失败步骤起的剩余计划」的新步骤列表。"""
        system = REPLAN_SYSTEM_PROMPT % {"actions": ", ".join(sorted(known_actions()))}
        user_prompt = (
            f"目标：{run.goal}\n"
            f"原计划：{json.dumps(run.plan or [], ensure_ascii=False, default=str)}\n"
            f"失败步骤：{json.dumps(failed_step, ensure_ascii=False, default=str)}\n"
            f"失败诊断：{diagnosis}"
        )
        return await self._ask_for_steps(run, system, user_prompt)
