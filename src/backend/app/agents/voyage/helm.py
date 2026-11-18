"""Helm（掌舵 · executor）：执行单个步骤，产出 observation。

异常不外抛：捕获为 ``observation["error"]``，交由 Sextant 判定失败并回传 Navigator。
"""

from typing import Any

from app.agents.voyage.actions import ActionContext, get_action
from app.agents.voyage.errorsig import error_text
from app.agents.voyage.failures import classify_failure
from app.services.managed_commands import failure_from_exception


class Helm:
    async def execute(self, ctx: ActionContext, step_def: dict[str, Any]) -> dict[str, Any]:
        action_name = str(step_def.get("action", ""))
        action = get_action(action_name)
        if action is None:
            return {"error": f"unknown action: {action_name}"}
        try:
            return await action(ctx, step_def.get("params") or {})
        except Exception as e:  # noqa: BLE001 —— 步骤失败要落 observation 而非炸掉状态机
            report = failure_from_exception(e)
            # code / transient：引擎据此决定停下来之后要不要自己过一会儿再试
            found = classify_failure(e)
            failure = report.to_dict() | {"code": found.code, "transient": found.transient}
            if found.user_message:
                # 异常自带的给人看的说明：任务停下来时用它，而不是按错误码查不到的兜底文案
                failure["user_message"] = found.user_message
            return {"error": error_text(e), "failure": failure}
