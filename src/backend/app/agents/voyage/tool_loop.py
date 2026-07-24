"""通用的有界工具循环：把只读检索工具接进任意 voyage 动作的 LLM 生成。

从 ``actions_proposal._tool_loop`` 提取、泛化：调用方给定一批允许的工具名
（``tool_names``）与已注入工具规格的 ``system`` prompt，LLM 每轮输出
``{"tool":..,"args":..}``（执行工具、结果回喂续跑）或 ``{"finish":..}``（返回）。

- 工具经统一注册表 ``app.tools`` 派发，``ToolContext`` 由 ``ActionContext`` 收窄而来；
- 只允许 ``tool_names`` 子集，越界工具当作错误消息回给 LLM（不打断循环）；
- 轮次耗尽后追加若干次「立即 finish」请求，仍不 finish 抛 ``ValueError``。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.actions_ideas import _extract_json
from app.core.llm.base import Message
from app.tools import ToolContext, get_tool, result_payload, run_tool

_TOOL_RESULT_CHARS = 4000  # 单个工具结果注入 prompt 的截断
_FORCE_FINISH_ATTEMPTS = 2  # 轮次耗尽后强制 finish 的补充尝试


def tool_context_from_action(ctx: ActionContext) -> ToolContext:
    """把 Voyage 的 ActionContext 收窄成只读工具需要的 ToolContext。"""
    return ToolContext(
        project_id=ctx.run.project_id,
        llm=ctx.llm,
        user_id=ctx.run.created_by,
        voyage_id=ctx.run.id,
    )


async def run_tool_loop(
    ctx: ActionContext,
    *,
    stage: str,
    system: str,
    opening: str,
    tool_names: list[str],
    max_calls: int,
    label: str,
    result_chars: int = _TOOL_RESULT_CHARS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """LLM 决策循环：``{"tool":..}`` 执行工具续跑；``{"finish":..}`` 返回 (finish, trace)。"""
    allowed = set(tool_names)
    tctx = tool_context_from_action(ctx)
    messages: list[Message] = [
        Message(role="system", content=system),
        Message(role="user", content=opening),
    ]
    trace: list[dict[str, Any]] = []

    async def ask() -> Any:
        result = await ctx.llm.complete(
            stage,
            messages,
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            library_id=ctx.run.library_id,
            voyage_id=ctx.run.id,
        )
        messages.append(Message(role="assistant", content=result.content))
        try:
            return _extract_json(result.content)
        except (ValueError, json.JSONDecodeError):
            return None

    for _round in range(max_calls):
        decision = await ask()
        if isinstance(decision, dict) and isinstance(decision.get("finish"), dict):
            return decision["finish"], trace
        if isinstance(decision, dict) and decision.get("tool"):
            tool_name = str(decision["tool"])
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            try:
                if tool_name not in allowed:
                    raise ValueError(
                        f"未授权工具：{tool_name}（本环节可用：{', '.join(sorted(allowed))}）"
                    )
                result = await run_tool(tctx, tool_name, args)
                data = result_payload(result)  # 内部循环只用文本 payload，忽略图片
                payload = json.dumps(data, ensure_ascii=False, default=str)
                spec = get_tool(tool_name)
                summary = spec.summary(args, data) if spec else tool_name
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 工具错误回给 LLM 继续探索
                payload = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                summary = f"{tool_name} 出错：{e}"
            trace.append({"tool": tool_name, "args": args, "summary": summary})
            await ctx.log(f"{label}：{summary}")
            messages.append(Message(role="user", content=f"工具结果：\n{payload[:result_chars]}"))
            continue
        messages.append(
            Message(role="user", content="上一条输出不合法。只输出 tool 或 finish 的 JSON 对象。")
        )

    for _attempt in range(_FORCE_FINISH_ATTEMPTS):
        messages.append(
            Message(role="user", content="探索轮次已用尽，请立即输出 finish JSON，不要再调用工具。")
        )
        decision = await ask()
        if isinstance(decision, dict) and isinstance(decision.get("finish"), dict):
            return decision["finish"], trace
    raise ValueError(f"{label}：LLM 在轮次耗尽后仍未交付 finish")
