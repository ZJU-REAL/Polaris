"""统一只读工具注册表：单一事实源。

每个工具 = 一个 ``ToolSpec``（名字 + 描述 + JSON Schema 入参 + handler）。
- 内部 agent 走 ``tool_loop.run_tool_loop``：把 ``render_tool_specs`` 注入 system prompt，
  LLM 输出 ``{"tool":..,"args":..}`` JSON，代码 ``run_tool`` 派发。
- 外部 MCP 服务器遍历 ``list_tools()``，把每个 ``input_schema`` 直接注册成 MCP tool。

工具 handler 只依赖 ``services/*``（不 import fastapi），全部只读。
未知工具 / 非 JSON 对象参数抛 ``ValueError``，由调用方转成回给 LLM 的错误消息。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.tools.context import ToolContext

ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
Summarizer = Callable[[dict[str, Any], dict[str, Any]], str]


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """一个只读检索工具的完整声明。"""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema（object），MCP 直接复用
    handler: ToolHandler
    read_only: bool = True
    network: bool = False  # True = 访问外部 HTTP API（arxiv/S2/OpenAlex）
    summarize: Summarizer | None = None  # 生成一句人读的调用摘要（日志用）

    def summary(self, args: dict[str, Any], result: dict[str, Any]) -> str:
        if self.summarize is not None:
            try:
                return self.summarize(args, result)
            except Exception:  # noqa: BLE001 — 摘要失败不该打断循环
                pass
        return self.name


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str,
    *,
    description: str,
    input_schema: dict[str, Any],
    read_only: bool = True,
    network: bool = False,
    summarize: Summarizer | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """把一个 ``async (ToolContext, args) -> dict`` handler 注册为工具。"""

    def decorator(func: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise ValueError(f"工具重复注册：{name}")
        _REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
            read_only=read_only,
            network=network,
            summarize=summarize,
        )
        return func

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def list_tools(names: list[str] | None = None) -> list[ToolSpec]:
    """按注册顺序返回工具（可按名字子集过滤，保持给定顺序）。"""
    if names is None:
        return list(_REGISTRY.values())
    out: list[ToolSpec] = []
    for n in names:
        spec = _REGISTRY.get(n)
        if spec is not None:
            out.append(spec)
    return out


def known_tools() -> frozenset[str]:
    return frozenset(_REGISTRY)


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行单个工具；未知工具 / 非法参数抛 ``ValueError``。"""
    spec = _REGISTRY.get(name)
    if spec is None:
        raise ValueError(f"未知工具：{name}（可用：{', '.join(sorted(_REGISTRY))}）")
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return await spec.handler(ctx, args)


def _arg_hint(name: str, schema: dict[str, Any], required: set[str]) -> str:
    """把单个入参渲染成紧凑提示，如 ``"mode": "keyword"|"semantic"``。"""
    if "enum" in schema:
        rendered = "|".join(f'"{v}"' for v in schema["enum"])
    else:
        rendered = {
            "string": '"..."',
            "integer": "int",
            "number": "num",
            "boolean": "bool",
        }.get(schema.get("type", "string"), "...")
    key = f'"{name}"' if name in required else f'"{name}"?'
    return f"{key}: {rendered}"


def render_tool_specs(names: list[str] | None = None) -> str:
    """由注册表生成注入 system prompt 的紧凑工具规格（替代手写 TOOL_SPECS）。"""
    lines: list[str] = []
    for spec in list_tools(names):
        props: dict[str, Any] = spec.input_schema.get("properties", {}) or {}
        required = set(spec.input_schema.get("required", []) or [])
        args = ", ".join(_arg_hint(n, s, required) for n, s in props.items())
        lines.append(f"- {spec.name} {{{args}}}：{spec.description}")
    return "\n".join(lines)
