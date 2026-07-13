"""确定性假 Provider：无需网络与 API key，测试与无 key 演示用。

对 prompt 做简单模板回显；识别到 JSON 请求（Navigator 规划 / Sextant 判定的
system prompt 标记）时返回符合对应 schema 的合法 JSON。
"""

import json
from collections.abc import AsyncIterator, Sequence

from app.core.llm.base import CompletionResult, LLMProvider, Message

# 与 navigator.py / sextant.py 的 system prompt 对齐的识别标记
_PLAN_MARKER = '"steps"'
_VERDICT_MARKER = '"passed"'

_FAKE_PLAN = {
    "steps": [
        {
            "title": "分析目标",
            "action": "llm.complete",
            "params": {"stage": "default", "prompt": "围绕目标给出分析要点：{goal}"},
            "acceptance": "输出包含对目标的分析要点",
        }
    ]
}


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（len/4，最少 1）。"""
    return max(1, len(text) // 4)


class FakeProvider(LLMProvider):
    name = "fake"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        content = self._respond(messages, model)
        prompt_len = sum(estimate_tokens(m.content) for m in messages)
        return CompletionResult(
            content=content,
            model=model,
            finish_reason="stop",
            usage={
                "prompt_tokens": prompt_len,
                "completion_tokens": estimate_tokens(content),
            },
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        result = await self.complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        chunk = 64
        for i in range(0, len(result.content), chunk):
            yield result.content[i : i + chunk]

    @staticmethod
    def _respond(messages: Sequence[Message], model: str) -> str:
        full_text = "\n".join(m.content for m in messages)
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            full_text,
        )
        if _VERDICT_MARKER in full_text and _PLAN_MARKER not in full_text:
            return json.dumps(
                {"passed": True, "reason": "fake-sextant: 产出满足验收标准（确定性假判定）"},
                ensure_ascii=False,
            )
        if _PLAN_MARKER in full_text:
            return json.dumps(_FAKE_PLAN, ensure_ascii=False)
        return f"[fake:{model}] {last_user[:400]}"
