"""LLM 抽象层。

业务代码只允许依赖本模块的 ``LLMProvider`` 接口（complete/stream/stream_events），
不允许直接 import openai/anthropic SDK。具体实现见 openai_compat.py / anthropic.py。

关于 content blocks
-------------------
``Message.content`` 从 ``str`` 放宽成 ``str | list[ContentBlock]``，是为了装下工具调用
（模型说"我要调 X"）与工具结果（"X 返回了这些"）。放宽而不是重做，是因为全仓有 71 处
``Message(role=..., content="...")`` 的构造点，而 ``.content`` 的**读取点只有 12 处**且
全部集中在本包内——所以加一个 ``.text`` 属性、只改读取点，71 处构造一行不动。

工具结果**不用 ``role="tool"``**，而是以 ``ToolResultBlock`` 装在 ``role="user"`` 的消息
里（Anthropic 的形状）。三个理由：一轮并行 N 个结果天然装在一条消息里，不用编造 N 条
消息的顺序约定；结果里能带图片（figures 那组工具会返图），而 OpenAI 的 ``role:"tool"``
消息在多数中转上是纯文本；从这个形状适配 OpenAI 是确定性扇出（一条 → K 条 ``role:
"tool"``），反过来则要往回看相邻消息做归并，脆得多。
"""

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

Role = str  # "system" | "user" | "assistant"

# 推理档位（思考深度）。取值取 OpenAI 与 Anthropic 两家的并集，具体模型支持哪些子集
# 由服务端校验（各家对不支持的档位会返回明确的 400，比在这里硬编码白名单更靠谱）。
EffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


# ---- content blocks ----


@dataclass(slots=True, frozen=True)
class TextBlock:
    text: str


@dataclass(slots=True, frozen=True)
class ImageBlock:
    data: bytes
    mime: str = "image/png"
    label: str | None = None


@dataclass(slots=True, frozen=True)
class ThinkingBlock:
    """模型的思考过程。

    ``signature`` 必须原样带回：Anthropic 的 thinking 块回放时缺签名会 400。所以历史
    里的 assistant 轮要用 ``CompletionResult.as_assistant_message()`` 重建，不能拿
    ``content`` 字符串手工拼——那样一定丢签名。
    """

    text: str
    signature: str | None = None


@dataclass(slots=True, frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str  # JSON 序列化后的文本 payload
    images: tuple[ImageBlock, ...] = ()
    is_error: bool = False


ContentBlock = TextBlock | ImageBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


def _block_text(block: ContentBlock) -> str:
    """块拍平成文本时的表示。非文本块给一个占位摘要，别在日志/估算里凭空消失。"""
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return block.text
    if isinstance(block, ToolUseBlock):
        return f"[调用 {block.name} {json.dumps(block.input, ensure_ascii=False)}]"
    if isinstance(block, ToolResultBlock):
        return f"[工具结果] {block.content}"
    return f"[图片 {block.mime}]"


@dataclass(slots=True)
class Message:
    role: Role
    content: str | list[ContentBlock]

    @property
    def text(self) -> str:
        """拍平成纯文本。字符串原样返回，块列表按块拼接。

        token 估算、调用日志、以及所有只关心"说了什么"的地方都用它，不要再读
        ``.content``——那个字段现在可能是列表。
        """
        if isinstance(self.content, str):
            return self.content
        return "\n".join(_block_text(b) for b in self.content)

    @property
    def blocks(self) -> tuple[ContentBlock, ...]:
        """统一成块列表。字符串包成单个 TextBlock。"""
        if isinstance(self.content, str):
            return (TextBlock(self.content),)
        return tuple(self.content)


@dataclass(slots=True)
class CompletionResult:
    content: str  # 拼好的纯文本；19 个 stage 只读它，语义不变
    model: str
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens/completion_tokens/...
    blocks: tuple[ContentBlock, ...] = ()

    @property
    def tool_calls(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))

    def as_assistant_message(self) -> Message:
        """回喂历史时用它，不要拿 ``content`` 手工拼消息。

        原始块里带着 thinking 的签名与 tool_use 的 id，丢了任何一个，下一轮请求都可能
        被 provider 拒掉。
        """
        if not self.blocks:
            return Message(role="assistant", content=self.content)
        return Message(role="assistant", content=list(self.blocks))


# ---- 流式事件 ----


@dataclass(slots=True, frozen=True)
class TextDelta:
    text: str


@dataclass(slots=True, frozen=True)
class ThinkingDelta:
    text: str
    signature: str | None = None


@dataclass(slots=True, frozen=True)
class ToolUseStart:
    index: int
    id: str
    name: str


@dataclass(slots=True, frozen=True)
class ToolUseArgsDelta:
    index: int
    json_fragment: str


@dataclass(slots=True, frozen=True)
class ToolUseStop:
    index: int


@dataclass(slots=True, frozen=True)
class StreamDone:
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


StreamEvent = TextDelta | ThinkingDelta | ToolUseStart | ToolUseArgsDelta | ToolUseStop | StreamDone


def normalize_finish_reason(raw: str | None) -> str | None:
    """两家的结束原因归一化到 tool_use / max_tokens / stop，上层不要 switch 原始值。"""
    if raw is None:
        return None
    if raw in ("tool_calls", "tool_use"):
        return "tool_use"
    if raw in ("length", "max_tokens"):
        return "max_tokens"
    if raw in ("stop", "end_turn", "stop_sequence"):
        return "stop"
    return raw


class ToolsUnsupportedError(RuntimeError):
    """这个模型/中转不认 tools 参数。由调用方降级（退回无工具的一次性问答）。"""


@dataclass(slots=True)
class RerankResult:
    """重排结果：(文档下标, 相关性分) 按分数降序；usage 供路由器记账（可空）。"""

    results: list[tuple[int, float]]
    usage: dict[str, int] = field(default_factory=dict)  # total_tokens（billed_units）等


class LLMProvider(ABC):
    """异步 LLM Provider 接口。"""

    name: str = "base"

    #: 支持原生工具调用吗。默认 False —— 未重写 ``stream_events`` 的 provider（含所有
    #: 测试替身）走下面那个只吐文本的默认实现，行为与今天完全一致。
    supports_tools: ClassVar[bool] = False

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResult:
        """一次性补全，返回完整结果。

        ``images``：可选 PNG 字节列表（多模态输入，附在最后一条 user 消息上）；
        不支持视觉输入的 provider 抛 NotImplementedError。
        ``effort``：推理档位，None = 不发送该参数（用模型默认）。
        ``tools``：``{name, description, parameters}`` 形式的工具定义（parameters 是标准
        JSON Schema），None = 不发送。
        """

    @abstractmethod
    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> AsyncIterator[str]:
        """流式补全，逐段 yield 文本增量（用于 SSE 转发）。

        签名保持不变（3 个 SSE 端点 + 写作辅助都靠它），**不给它加 tools 参数**；
        需要工具的走 :meth:`stream_events`。
        """

    async def stream_events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """结构化流式：文本增量、思考增量、工具调用的开始/参数分片/结束。

        默认实现包一层 :meth:`stream` 只吐 ``TextDelta`` + ``StreamDone``——所以没有
        重写它的 provider（以及测试里的各种替身）零改动仍然可用，只是拿不到工具。
        """
        async for chunk in self.stream(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        ):
            yield TextDelta(chunk)
        yield StreamDone(finish_reason="stop")

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        """文本嵌入（语义检索用）。不支持的 provider（如 anthropic）抛 NotImplementedError。"""
        raise NotImplementedError(f"provider {self.name!r} does not support embeddings")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        model: str,
        top_n: int | None = None,
    ) -> RerankResult:
        """重排：results 为 (documents 下标, 相关性分) 降序，top_n 截断（None 不截断）。

        不支持的 provider（如 anthropic）抛 NotImplementedError。
        """
        raise NotImplementedError(f"provider {self.name!r} does not support rerank")
