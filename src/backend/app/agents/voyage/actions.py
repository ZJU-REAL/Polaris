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

from app.agents.voyage.skillset import skill_guidance, skill_personas
from app.core.events import EventBus
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun


@dataclass(slots=True)
class ActionContext:
    """动作执行上下文：run 元数据 + LLM 路由器 + 可变 checkpoint 工作区 + 事件总线。"""

    run: VoyageRun
    llm: LLMRouter
    checkpoint: dict[str, Any] = field(default_factory=dict)
    bus: EventBus | None = None  # 动作内实时事件（如 review.message）；无总线时静默跳过
    step_id: Any | None = None  # 当前节点 id（动作查自己的闸门等；engine 注入）

    async def notify(self, message: dict[str, Any]) -> None:
        """向项目通知频道发布事件（bus 未注入 / 无起源课题时为 no-op）。"""
        # P9a：独立库任务无 project_id，无项目通知频道，静默跳过。
        if self.bus is not None and self.run.project_id is not None:
            await self.bus.publish_notify(self.run.project_id, message)

    async def log(self, message: str, *, level: str = "info") -> None:
        """向本任务日志频道发一条进度日志（任务详情页 terminal 消费）。

        供批处理动作播报逐项进度（如"打分 12/50: <标题>"）；bus 未注入时 no-op。
        level 同 engine._emit_log（info/step/success/error/plan/budget/gate）。
        """
        if self.bus is not None:
            await self.bus.publish_voyage_event(
                self.run.id,
                "log",
                {
                    "message": message,
                    "level": level,
                    "step_id": str(self.step_id) if self.step_id else None,
                },
            )

    def skill_guidance(self, *targets: str) -> str:
        """注入点上项目启用技能的补充指引（docs/skill-system.md §3）；无技能返回空串。"""
        return skill_guidance(self.checkpoint, *targets)

    def skill_personas(self, target: str) -> list[dict[str, Any]] | None:
        """persona 技能人设列表；None = 用调用方内置默认。"""
        return skill_personas(self.checkpoint, target)


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
        library_id=ctx.run.library_id,
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
