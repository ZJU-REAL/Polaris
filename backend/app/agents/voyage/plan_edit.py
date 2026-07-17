"""计划编辑操作集（docs/voyage-loop.md §5.3，阶段 D/E）。

计划不再靠「替换尾部」演化，而是对带状态位的扁平清单做**受限的增量编辑**：

    {"reason": "...", "edits": [
        {"op": "add_nodes", "insert_after": "<step_id>|null", "nodes": [<步骤定义>]},
        {"op": "update_node", "step_id": "...", "params": {...}, "acceptance": "...",
         "checks": [...]},
        {"op": "obsolete_nodes", "step_ids": [...], "reason": "..."}
    ]}
    或 {"finish": true, "reason": "..."}（建议按当前结果收束，仍须过 done_criteria）

两个生产者共用同一套校验与应用逻辑：
- Navigator LLM（loop 模式失败回灌，navigator.on_result）；
- 确定性分支表（kind 注册，如 experiment 的 reflection decision → 追加轮次节点），
  能写成规则的决策不问 LLM。

硬校验不变量：action 在注册表内、每个新节点必须带 acceptance 或 checks、
单次编辑新增节点数上限、只能编辑/作废非终态节点（应用期在 engine 校验）。
"""

from collections.abc import Callable
from typing import Any

MAX_NODES_PER_EDIT = 8

PLAN_EDIT_OPS = frozenset({"add_nodes", "update_node", "obsolete_nodes"})


class PlanEditError(Exception):
    """计划编辑非法（schema 错误 / 引用不存在或已终态的节点等）。"""


def validate_plan_edit(
    data: Any, *, step_validator: Callable[[Any], list[dict[str, Any]]]
) -> dict[str, Any]:
    """校验并规范化一次计划编辑；非法抛 ValueError（供 LLM 输出重试）。

    ``step_validator`` 复用 navigator.validate_steps（含 action 注册表校验与
    checks 缺省补齐），避免两套步骤校验漂移。
    """
    if not isinstance(data, dict):
        raise ValueError("plan edit must be an object")
    if data.get("finish") is True:
        return {"finish": True, "reason": str(data.get("reason", "")), "edits": []}
    raw_edits = data.get("edits")
    if not isinstance(raw_edits, list):
        raise ValueError('plan edit must contain "edits" list or "finish": true')
    edits: list[dict[str, Any]] = []
    new_nodes = 0
    for i, raw in enumerate(raw_edits):
        if not isinstance(raw, dict):
            raise ValueError(f"edit {i} is not an object")
        op = raw.get("op")
        if op not in PLAN_EDIT_OPS:
            raise ValueError(f"edit {i} has unknown op: {op!r}")
        if op == "add_nodes":
            nodes = step_validator({"steps": raw.get("nodes")})
            for node in nodes:
                if not node.get("acceptance") and not node.get("checks"):
                    raise ValueError(
                        f"edit {i}: new node {node['title']!r} 缺少验收（acceptance/checks）"
                    )
            new_nodes += len(nodes)
            insert_after = raw.get("insert_after")
            if insert_after is not None and not isinstance(insert_after, str):
                raise ValueError(f"edit {i} insert_after must be step_id string or null")
            edits.append({"op": "add_nodes", "insert_after": insert_after, "nodes": nodes})
        elif op == "update_node":
            step_id = raw.get("step_id")
            if not isinstance(step_id, str) or not step_id:
                raise ValueError(f"edit {i} update_node requires step_id")
            patch: dict[str, Any] = {}
            if isinstance(raw.get("params"), dict):
                patch["params"] = raw["params"]
            if isinstance(raw.get("title"), str) and raw["title"].strip():
                patch["title"] = raw["title"].strip()
            if isinstance(raw.get("acceptance"), str):
                patch["acceptance"] = raw["acceptance"]
            if isinstance(raw.get("checks"), list):
                patch["checks"] = raw["checks"]
            if not patch:
                raise ValueError(f"edit {i} update_node has no patch fields")
            edits.append({"op": "update_node", "step_id": step_id, "patch": patch})
        else:  # obsolete_nodes
            step_ids = raw.get("step_ids")
            if not isinstance(step_ids, list) or not step_ids:
                raise ValueError(f"edit {i} obsolete_nodes requires non-empty step_ids")
            edits.append(
                {
                    "op": "obsolete_nodes",
                    "step_ids": [str(s) for s in step_ids],
                    "reason": str(raw.get("reason", "")),
                }
            )
    if new_nodes > MAX_NODES_PER_EDIT:
        raise ValueError(f"too many new nodes in one edit: {new_nodes} > {MAX_NODES_PER_EDIT}")
    if not edits:
        raise ValueError("plan edit has no effective edits (use finish for wrap-up)")
    return {"finish": False, "reason": str(data.get("reason", "")), "edits": edits}


# ---- 确定性分支表：plan_signal → 计划编辑（不经 LLM，docs/voyage-loop.md §7） ----


def experiment_round_nodes(next_round: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"第 {next_round} 轮运行",
            "action": "experiment.run",
            "params": {"round": next_round},
            "acceptance": "本轮运行已结束并解析主指标（失败轮交由分析步骤诊断）",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        },
        {
            "title": f"第 {next_round} 轮分析",
            "action": "experiment.analyze",
            "params": {"round": next_round},
            "acceptance": "reflection 已落库并给出继续/收束判定",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        },
    ]


def experiment_wrapup_nodes() -> list[dict[str, Any]]:
    return [
        {
            "title": "实验图表（脚本生成 + 自动质检）",
            "action": "experiment.figures",
            "params": {},
            "acceptance": "figures 已生成、拉回本地并写入 Experiment.figures",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        },
        {
            "title": "实验报告（LLM）",
            "action": "experiment.report",
            "params": {},
            "acceptance": "markdown 报告已写入 Experiment.report",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        },
    ]


def experiment_signal_edits(
    signal: dict[str, Any], active_rows: list[Any]
) -> dict[str, Any] | None:
    """experiment.analyze 的 plan_signal → 追加节点（幂等：待办节点已存在则跳过）。

    - decision continue（improve/debug 已改完代码）→ 追加下一轮 run + analyze；
    - decision finish（终止条件命中）→ 追加 figures + report 收尾。
    """
    pending_actions = {r.action for r in active_rows if r.status != "passed"}
    decision = str(signal.get("decision") or "")
    if decision == "continue":
        if "experiment.run" in pending_actions:
            return None
        next_round = int(signal.get("next_round") or 0)
        return {
            "finish": False,
            "reason": f"继续迭代：第 {next_round} 轮",
            "edits": [
                {
                    "op": "add_nodes",
                    "insert_after": None,
                    "nodes": experiment_round_nodes(next_round),
                }
            ],
        }
    if decision == "finish":
        if "experiment.report" in pending_actions:
            return None
        return {
            "finish": False,
            "reason": f"迭代结束（{signal.get('stopped_reason') or ''}），进入收尾",
            "edits": [
                {"op": "add_nodes", "insert_after": None, "nodes": experiment_wrapup_nodes()}
            ],
        }
    return None


# kind → plan_signal 分支表（engine 在节点通过后查表应用；返回 None = 无编辑）
SIGNAL_TABLES: dict[str, Callable[[dict[str, Any], list[Any]], dict[str, Any] | None]] = {
    "experiment": experiment_signal_edits,
}
