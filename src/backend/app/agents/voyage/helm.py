"""Helm（掌舵 · executor）：执行单个步骤，产出 observation。

异常不外抛：捕获为 ``observation["error"]``，交由 Sextant 判定失败并回传 Navigator。
"""

from typing import Any

from app.agents.voyage.actions import ActionContext, get_action
from app.agents.voyage.errorsig import error_text
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
            return {"error": error_text(e), "failure": report.to_dict()}
