"""对话 agent 循环产出的事件。

循环自己不认识 HTTP，也不认识 SSE——它只吐这些事件，由 API 层翻成线上帧。这条边界
是可测性的来源：脱离 HTTP 就能用脚本化的 FakeProvider 把整个循环跑穿。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class MetaEvent:
    """首帧：这轮属于哪个会话、用了什么模型、带了哪些工具。"""

    conversation_id: str
    message_id: str
    model: str
    tools: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SourcesEvent:
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class DeltaEvent:
    """正文增量。**形状与今天的 delta 帧一字不差**，老客户端照常工作。"""

    text: str


@dataclass(slots=True, frozen=True)
class ThinkingEvent:
    text: str


@dataclass(slots=True, frozen=True)
class PlanEvent:
    """当前的任务计划。``update_plan`` 调成功后由循环补发。

    它是 ``tool_result`` 的**衍生帧**，不是新的信息源：同一份计划已经在工具结果里
    回喂给模型了，这一帧只是让前端不必去解析工具结果的 JSON 就能画出进度。
    """

    steps: tuple[dict[str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class ToolCallEvent:
    id: str
    name: str
    args: dict[str, Any]
    read_only: bool = True


@dataclass(slots=True, frozen=True)
class ToolResultEvent:
    """工具结果。

    ``preview`` 是**截断过的**：完整 payload 走消息详情接口按需取。一次
    ``get_fact_pack`` 的原始返回能把连接和浏览器一起灌爆。
    """

    id: str
    name: str
    ok: bool
    summary: str
    preview: str
    duration_ms: int
    images: int = 0


@dataclass(slots=True, frozen=True)
class UsageEvent:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True, frozen=True)
class CompactionEvent:
    removed: int
    reason: str = "tool_results_elided"


@dataclass(slots=True, frozen=True)
class DoneEvent:
    """收尾。``stop_reason`` 让界面能说人话，而不是都显示成"完成"。"""

    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "stop"
    message_id: str = ""


@dataclass(slots=True, frozen=True)
class ErrorEvent:
    detail: str
    code: str = "CHAT_FAILED"
    recoverable: bool = False


ChatEvent = (
    MetaEvent
    | SourcesEvent
    | DeltaEvent
    | ThinkingEvent
    | PlanEvent
    | ToolCallEvent
    | ToolResultEvent
    | UsageEvent
    | CompactionEvent
    | DoneEvent
    | ErrorEvent
)
