"""Voyage 动作注册表：Helm 按 ``action`` 名查表执行。

M1 动作：
- ``llm.complete``：按 stage 路由调用 LLM，prompt 支持 ``{goal}`` 等模板变量
- ``sleep``：等待 N 秒（测试/演示用）
- ``artifact.write``：把文本产物写入 run.checkpoint["artifacts"]
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun


@dataclass(slots=True)
class ActionContext:
    """动作执行上下文：run 元数据 + LLM 路由器 + 可变 checkpoint 工作区。"""

    run: VoyageRun
    llm: LLMRouter
    checkpoint: dict[str, Any] = field(default_factory=dict)


ActionFunc = Callable[[ActionContext, dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, ActionFunc] = {}


def register(name: str) -> Callable[[ActionFunc], ActionFunc]:
    def decorator(func: ActionFunc) -> ActionFunc:
        _REGISTRY[name] = func
        return func

    return decorator


def get_action(name: str) -> ActionFunc | None:
    return _REGISTRY.get(name)


def known_actions() -> frozenset[str]:
    return frozenset(_REGISTRY)


class _SafeDict(dict):
    """format_map 时缺失的变量原样保留（``{missing}``），不抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, ctx: ActionContext, params: dict[str, Any]) -> str:
    mapping = _SafeDict({"goal": ctx.run.goal, "kind": ctx.run.kind})
    extra = params.get("vars")
    if isinstance(extra, dict):
        mapping.update(extra)
    return template.format_map(mapping)


@register("llm.complete")
async def llm_complete(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    stage = params.get("stage", "default")
    prompt = render_template(str(params.get("prompt", "")), ctx, params)
    if not prompt:
        raise ValueError("llm.complete requires a non-empty prompt")
    messages: list[Message] = []
    if system := params.get("system"):
        messages.append(Message(role="system", content=str(system)))
    messages.append(Message(role="user", content=prompt))
    result = await ctx.llm.complete(
        stage,
        messages,
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        voyage_id=ctx.run.id,
    )
    return {"content": result.content, "model": result.model, "usage": result.usage}


@register("sleep")
async def sleep_action(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    seconds = float(params.get("seconds", 0))
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    await asyncio.sleep(min(seconds, 5.0))  # 上限防误配长眠
    return {"slept": seconds}


@register("artifact.write")
async def artifact_write(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "artifact.txt")
    content = render_template(str(params.get("content", "")), ctx, params)
    if not content:
        raise ValueError("artifact.write requires non-empty content")
    artifacts = dict(ctx.checkpoint.get("artifacts") or {})
    artifacts[name] = content
    ctx.checkpoint["artifacts"] = artifacts
    return {"artifact": name, "size": len(content)}
