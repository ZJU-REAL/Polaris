"""通用的有界工具循环：把只读检索工具接进任意 voyage 动作的 LLM 生成。

从 ``actions_proposal._tool_loop`` 提取、泛化：调用方给定一批允许的工具名
（``tool_names``）、已注入工具规格的 ``system`` prompt 和交付物的 schema
（``finish_schema``）。LLM 每轮要么调一个工具（执行、结果回喂续跑），要么交付
``finish``（返回）。

- 每轮走 ``LLMRouter.complete_json``：回答的形状由结构化输出保证，交付物在这里就
  按 ``finish_schema`` 校验过；格式修不好、写不完、拒答时抛 ``LLMError``，由调用方
  决定怎么降级，网络/限流这类失败同样原样上抛；
- 工具经统一注册表 ``app.tools`` 派发，``ToolContext`` 由 ``ActionContext`` 收窄而来；
- 只允许 ``tool_names`` 子集，越界工具当作错误消息回给 LLM（不打断循环）；
- 轮次耗尽后追加若干次「立即 finish」请求，仍不 finish 抛 ``ValueError``。
"""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any, Self

from pydantic import BaseModel, create_model, model_validator

from app.agents.voyage.actions import ActionContext
from app.core.llm.base import Message
from app.tools import ToolContext, get_tool, result_payload, run_tool

_TOOL_RESULT_CHARS = 4000  # 单个工具结果注入 prompt 的截断
_FORCE_FINISH_ATTEMPTS = 2  # 轮次耗尽后强制 finish 的补充尝试

# 「每轮怎么回答」的说明，调用方拼进自己的 system prompt（交付物的字段另行说明）
TURN_FORMAT = """\
每轮二选一：
1. 调用工具：tool 填工具名，args 填参数对象序列化成的 JSON 字符串
   （例如 "{\\"query\\": \\"检索词\\", \\"k\\": 5}"），finish 填 null；
2. 交付：finish 填交付内容，tool 与 args 填 null。"""


class _ToolTurn(BaseModel):
    """工具循环里模型一轮的回答；``finish`` 字段由 :func:`_turn_schema` 按调用方补上。

    ``args`` 是 JSON 字符串而不是对象：每个工具的参数各不相同，注册表还能再加工具，
    写不成一个封闭的 schema。
    """

    tool: str | None
    args: str | None

    @model_validator(mode="after")
    def _tool_or_finish(self) -> Self:
        if not (self.tool or "").strip() and getattr(self, "finish", None) is None:
            raise ValueError("set either `tool` (with `args`) or `finish`")
        return self


@lru_cache(maxsize=32)
def _turn_schema(finish_schema: type[BaseModel]) -> type[_ToolTurn]:
    return create_model(
        f"{finish_schema.__name__}ToolTurn",
        __base__=_ToolTurn,
        finish=(finish_schema | None, ...),
    )


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    args = json.loads(raw)
    if not isinstance(args, dict):
        raise ValueError("args 必须是 JSON 对象")
    return args


def tool_context_from_action(ctx: ActionContext) -> ToolContext:
    """把 Voyage 的 ActionContext 收窄成只读工具需要的 ToolContext。"""
    return ToolContext(
        project_id=ctx.run.project_id,
        llm=ctx.llm,
        user_id=ctx.run.created_by,
        voyage_id=ctx.run.id,
    )


async def run_tool_loop[FinishT: BaseModel](
    ctx: ActionContext,
    *,
    stage: str,
    system: str,
    opening: str,
    tool_names: list[str],
    max_calls: int,
    label: str,
    finish_schema: type[FinishT],
    result_chars: int = _TOOL_RESULT_CHARS,
) -> tuple[FinishT, list[dict[str, Any]]]:
    """LLM 决策循环：调工具就执行并续跑；交付 ``finish`` 就返回 (finish, trace)。"""
    allowed = set(tool_names)
    tctx = tool_context_from_action(ctx)
    schema = _turn_schema(finish_schema)
    messages: list[Message] = [
        Message(role="system", content=system),
        Message(role="user", content=opening),
    ]
    trace: list[dict[str, Any]] = []

    async def ask() -> Any:
        result = await ctx.llm.complete_json(
            stage,
            messages,
            schema=schema,
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            library_id=ctx.run.library_id,
            voyage_id=ctx.run.id,
        )
        messages.append(Message(role="assistant", content=result.data.model_dump_json()))
        return result.data

    for _round in range(max_calls):
        turn = await ask()
        if turn.finish is not None:
            return turn.finish, trace
        tool_name = str(turn.tool).strip()
        args: dict[str, Any] = {}
        try:
            args = _parse_args(turn.args)
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

    for _attempt in range(_FORCE_FINISH_ATTEMPTS):
        messages.append(
            Message(role="user", content="探索轮次已用尽，请立即输出 finish，不要再调用工具。")
        )
        turn = await ask()
        if turn.finish is not None:
            return turn.finish, trace
    raise ValueError(f"{label}：LLM 在轮次耗尽后仍未交付 finish")
