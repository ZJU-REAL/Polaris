"""Sextant（六分仪 · self-verification）：每步完成后对照验收标准"对星定位"。

判定顺序（docs/voyage-loop.md §6，规则优先、LLM 兜底）：
1. observation.error → 直接 fail；
2. 动作自带机械验收结论 self_check → 直接采信；
3. 步骤声明了结构化 checks → 检查注册表执行（确定性先跑，llm_rubric 最后）；
4. 无 checks 的遗留路径：确定性动作白名单（sleep/artifact.write）→ 技能
   output_contract → 验收标准文字走 LLM 判定 → 无标准但有产出默认通过。
"""

import json
from typing import Any

from app.agents.voyage.checks import run_deterministic_checks
from app.agents.voyage.skillset import check_output_contract, skill_output_contract
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun

_MAX_ATTEMPTS = 3  # 首次 + 重试 2 次

# 无需判定的 M1 基础动作（LLM 自由计划可用，无产出文本）
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

        # 动作自带机械验收结论（goal./proposal. 系动作，docs/api-idea2.md）：直接采信
        self_check = observation.get("self_check")
        if isinstance(self_check, dict) and isinstance(self_check.get("passed"), bool):
            return {
                "passed": self_check["passed"],
                "reason": str(self_check.get("reason", "")),
            }, {}

        # ---- 结构化验收（docs/voyage-loop.md §6）----
        checks = step_def.get("checks")
        if isinstance(checks, list) and checks:
            verdict, rubrics = run_deterministic_checks(
                checks, observation=observation, checkpoint=run.checkpoint
            )
            if verdict is not None:
                return verdict, {}
            usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
            for rubric in rubrics:
                verdict, usage = await self._judge(
                    run, step_def, observation, str(rubric.get("rubric") or "")
                )
                usage_total["prompt_tokens"] += usage.get("prompt_tokens", 0)
                usage_total["completion_tokens"] += usage.get("completion_tokens", 0)
                if not verdict["passed"]:
                    return verdict, usage_total
            return {"passed": True, "reason": "全部检查通过（含 LLM 判定）"}, usage_total

        # ---- 遗留路径（无 checks 声明的步骤）----
        action = str(step_def.get("action", ""))
        acceptance = step_def.get("acceptance")

        if action in DETERMINISTIC_ACTIONS:
            return {"passed": True, "reason": f"确定性步骤 {action} 执行成功"}, {}

        content = observation.get("content")
        if not content:
            return {"passed": False, "reason": "步骤无产出（observation.content 为空）"}, {}

        # 技能 output_contract：先做确定性校验（docs/skill-system.md §3.2），
        # 不通过直接 fail（不花 LLM），通过后仍走 LLM 对照验收标准
        contract = skill_output_contract(run.checkpoint, action)
        if contract and (error := check_output_contract(contract, str(content))):
            return {"passed": False, "reason": f"产出不符合技能约定：{error}"}, {}

        if not acceptance:
            return {"passed": True, "reason": "未声明验收标准，且步骤有产出，默认通过"}, {}

        return await self._judge(run, step_def, observation, str(acceptance))

    async def _judge(
        self,
        run: VoyageRun,
        step_def: dict[str, Any],
        observation: dict[str, Any],
        rubric: str,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """LLM 对照 rubric 判定产出（llm_rubric 检查与遗留验收文字共用）。"""
        content = observation.get("content")
        if content is None:
            content = json.dumps(observation, ensure_ascii=False, default=str)
        user_prompt = (
            f"步骤标题：{step_def.get('title', '')}\n"
            f"验收标准：{rubric}\n"
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
            {"passed": False, "reason": f"自动校验输出无法解析为合法判定 JSON：{last_error}"},
            usage_total,
        )
