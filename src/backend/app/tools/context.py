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

    - ``project_id``：默认的检索范围（课题关联的那些库）。
    - ``library_ids``：显式的检索范围，给了就**不看 project_id**。全局助手用它把范围
      放到「这个人看得见的全部库」；空元组是合法值，表示没有语料（查不到，不报错）。
    - ``user_id``：归属校验用（``*_for_user`` 服务）；系统内部调用可为 None。
    - ``voyage_id``：仅用于 LLM 用量记账（embed 调用），无则不挂 voyage。
    - ``llm``：LLM 路由器，仅供需要 embedding 的工具（语义检索）使用。
    """

    #: 课题范围。全局助手不收窄到课题时是 None——**不要编一个占位 uuid**：它会顺着
    #: embedding 记账写进 llm_usage.project_id，撞 FK 或者更糟，悄悄记到一个不存在的
    #: 课题头上。
    project_id: uuid.UUID | None
    llm: LLMRouter
    library_ids: tuple[uuid.UUID, ...] | None = None
    user_id: uuid.UUID | None = None
    voyage_id: uuid.UUID | None = None
    #: 生成可下载资源链接时使用。HTTP MCP 取当前 /mcp 请求的 origin；stdio 可由
    #: POLARIS_PUBLIC_BASE_URL 提供；内部工具调用没有地址时返回相对 URL。
    base_url: str | None = None
    #: 允许执行会改数据的工具吗。**默认 False 是整套安全性的支点**——MCP、voyage 的
    #: tool_loop、以及所有现存调用点因此自动保持只读，一行不用改。只有明确开了它的
    #: 调用方（走完审批的对话轮次）才拿得到写能力。
    allow_writes: bool = False
