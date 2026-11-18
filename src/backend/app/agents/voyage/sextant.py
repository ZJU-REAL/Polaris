"""Sextant（六分仪 · self-verification）：每步完成后对照验收标准"对星定位"。

判定顺序（docs/task-system.md §7（原 voyage-loop.md §6），规则优先、LLM 兜底）：
1. observation.error → 直接 fail；
2. 动作自带机械验收结论 self_check → 直接采信；
3. 步骤声明了结构化 checks → 检查注册表执行（确定性先跑，llm_rubric 最后）；
4. 无 checks 的遗留路径：确定性动作白名单（sleep/artifact.write）→ 验收标准
   文字走 LLM 判定 → 无标准但有产出默认通过。
"""

import json
from typing import Any

from pydantic import BaseModel

from app.agents.voyage.checks import run_deterministic_checks
from app.agents.voyage.errorsig import error_text
from app.core.llm.base import Message
from app.core.llm.errors import LLMSchemaError, LLMTruncatedError
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun

# 无需判定的 M1 基础动作（LLM 自由计划可用，无产出文本）
DETERMINISTIC_ACTIONS = frozenset({"sleep", "artifact.write"})

VERIFY_SYSTEM_PROMPT = """\
你是 Sextant，负责验证一个步骤的产出是否满足验收标准。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"passed": true 或 false, "reason": "简要理由"}
"""


class VerdictOut(BaseModel):
    """判定结果：过没过，加一句理由。"""

    passed: bool
    reason: str = ""


class SextantError(Exception):
    """判定用的模型调用本身失败了（网络、限流、没配模型……），不是「产出不合格」。

    两者必须分开：调用失败说明不了产出好坏，当成「未通过」会让引擎为一次网络抖动
    去重规划。原始异常挂在 ``__cause__`` 上，引擎据此归类并把任务停下来。"""


class Sextant:
    def __init__(self, llm: LLMRouter) -> None:
        self._llm = llm

    async def verify(
        self, run: VoyageRun, step_def: dict[str, Any], observation: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """返回 (verdict {passed, reason}, 本次验证消耗的 usage)。"""
        if observation.get("error"):
            return {"passed": False, "reason": str(observation["error"])}, {}

        # 动作自带机械验收结论（goal./proposal. 系动作，docs/task-system.md §7）：直接采信
        self_check = observation.get("self_check")
        if isinstance(self_check, dict) and isinstance(self_check.get("passed"), bool):
            return {
                "passed": self_check["passed"],
                "reason": str(self_check.get("reason", "")),
            }, {}

        # ---- 结构化验收（docs/task-system.md §7（原 voyage-loop.md §6））----
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
        try:
            # 结构化输出：格式不对的那一次重问由 complete_json 自己做
            result = await self._llm.complete_json(
                "sextant",
                [
                    Message(role="system", content=VERIFY_SYSTEM_PROMPT),
                    Message(role="user", content=user_prompt),
                ],
                schema=VerdictOut,
                user_id=run.created_by,
                project_id=run.project_id,
                voyage_id=run.id,
            )
        except (LLMSchemaError, LLMTruncatedError) as e:
            # 模型给不出一个合法的判定：和以前一样按「未通过」处理，交给重规划
            return {"passed": False, "reason": f"自动校验没有给出合法的判定：{e}"}, {}
        except Exception as e:  # noqa: BLE001 — 其余调用失败都要带着原因交给引擎
            raise SextantError(error_text(e)) from e
        usage = {
            "prompt_tokens": int(result.usage.get("prompt_tokens", 0)),
            "completion_tokens": int(result.usage.get("completion_tokens", 0)),
        }
        return {"passed": result.data.passed, "reason": result.data.reason}, usage
