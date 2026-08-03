"""工具执行上下文：把只读检索工具从 Voyage 的 ``ActionContext`` 解耦。

同一批工具既被内部 agent（tool_loop）调用，又被外部 MCP 服务器调用，
两条路径都构造一个轻量 ``ToolContext``（只带工具真正需要的字段），
工具内部照旧用 ``get_sessionmaker()`` 自开 session。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.llm.router import LLMRouter


@dataclass(slots=True)
class ToolContext:
    """只读工具的最小执行上下文。

    - ``project_id``：工具的检索范围（库内论文/概念/图谱都按项目隔离）。
    - ``user_id``：归属校验用（``*_for_user`` 服务）；系统内部调用可为 None。
    - ``voyage_id``：仅用于 LLM 用量记账（embed 调用），无则不挂 voyage。
    - ``llm``：LLM 路由器，仅供需要 embedding 的工具（语义检索）使用。
    """

    project_id: uuid.UUID
    llm: LLMRouter
    user_id: uuid.UUID | None = None
    voyage_id: uuid.UUID | None = None
    #: 允许执行会改数据的工具吗。**默认 False 是整套安全性的支点**——MCP、voyage 的
    #: tool_loop、以及所有现存调用点因此自动保持只读，一行不用改。只有明确开了它的
    #: 调用方（走完审批的对话轮次）才拿得到写能力。
    allow_writes: bool = False
