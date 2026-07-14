"""Sextant（六分仪 · self-verification）：每步完成后对照验收标准"对星定位"。

- 确定性步骤（sleep / artifact.write）规则判定；
- llm.complete 步骤用 stage=sextant 的 LLM 判定，输出严格 JSON
  {"passed": bool, "reason": str}，解析失败重试。
"""

import json
from typing import Any

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun

_MAX_ATTEMPTS = 3  # 首次 + 重试 2 次

# 无需 LLM 判断的确定性动作
DETERMINISTIC_ACTIONS = frozenset({"sleep", "artifact.write"})

VERIFY_SYSTEM_PROMPT = """\
你是 Sextant，负责验证一个步骤的产出是否满足验收标准。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"passed": true 或 false, "reason": "简要理由"}
"""


def _parse_verdict(content: str) -> dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    data = json.loads(content[start : end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
        raise ValueError("verdict must contain boolean 'passed'")
    return {"passed": data["passed"], "reason": str(data.get("reason", ""))}


class Sextant:
    def __init__(self, llm: LLMRouter) -> None:
        self._llm = llm

    async def verify(
        self, run: VoyageRun, step_def: dict[str, Any], observation: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """返回 (verdict {passed, reason}, 本次验证消耗的 usage)。"""
        if observation.get("error"):
            return {"passed": False, "reason": str(observation["error"])}, {}

        action = str(step_def.get("action", ""))
        acceptance = step_def.get("acceptance")

        if action == "experiment.smoke" and observation.get("exit_code") != 0:
            return {
                "passed": False,
                "reason": f"冒烟测试退出码 {observation.get('exit_code')} != 0",
            }, {}

        if action in DETERMINISTIC_ACTIONS or action.startswith(
            ("wiki.", "forge.", "review.", "experiment.")
        ):
            # wiki./forge./review./experiment. 为确定性批处理步骤：单条失败已汇总进
            # observation.failed，步骤级失败（helm 捕获的异常）走上面的 observation.error 分支
            return {"passed": True, "reason": f"确定性步骤 {action} 执行成功"}, {}

        content = observation.get("content")
        if not content:
            return {"passed": False, "reason": "步骤无产出（observation.content 为空）"}, {}
        if not acceptance:
            return {"passed": True, "reason": "未声明验收标准，且步骤有产出，默认通过"}, {}

        user_prompt = (
            f"步骤标题：{step_def.get('title', '')}\n"
            f"验收标准：{acceptance}\n"
            f"步骤产出：\n{str(content)[:4000]}"
        )
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        last_error: Exception | None = None
        for _attempt in range(_MAX_ATTEMPTS):
            result = await self._llm.complete(
                "sextant",
                [
                    Message(role="system", content=VERIFY_SYSTEM_PROMPT),
                    Message(role="user", content=user_prompt),
                ],
                user_id=run.created_by,
                project_id=run.project_id,
                voyage_id=run.id,
            )
            usage_total["prompt_tokens"] += int(result.usage.get("prompt_tokens", 0))
            usage_total["completion_tokens"] += int(result.usage.get("completion_tokens", 0))
            try:
                return _parse_verdict(result.content), usage_total
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
        return (
            {"passed": False, "reason": f"Sextant 输出无法解析为合法判定 JSON：{last_error}"},
            usage_total,
        )
