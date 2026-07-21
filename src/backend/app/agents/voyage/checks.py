"""结构化验收检查注册表（docs/voyage-loop.md §6）。

Sextant 从「LLM 裁判」变成「检查执行器」：确定性检查先跑、任一失败直接 fail
（不花 LLM），只有 ``llm_rubric`` 类检查交回 Sextant 走 LLM 判定。
失败 reason 必须 actionable（哪条 check、期望什么、实际什么）——它是重试与
重规划的诊断输入。

检查项格式（步骤 acceptance.checks 列表元素）::

    {"kind": "no_error"}
    {"kind": "exit_code", "value": 0}
    {"kind": "artifact_exists", "key": "artifacts.demo-report.md"}   # checkpoint 点路径
    {"kind": "schema_valid", "field": "plan", "required_keys": ["primary_metric"]}
    {"kind": "metric", "name": "accuracy", "op": ">=", "value": 0.8}
    {"kind": "min_count", "field": "papers", "value": 1}
    {"kind": "llm_rubric", "rubric": "分析是否覆盖了目标的关键问题"}
"""

from collections.abc import Callable
from typing import Any

DETERMINISTIC_CHECK_KINDS = frozenset(
    {"no_error", "exit_code", "artifact_exists", "schema_valid", "metric", "min_count"}
)
CHECK_KINDS = DETERMINISTIC_CHECK_KINDS | {"llm_rubric"}

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


def _dig(data: Any, dotted: str) -> Any:
    """点路径取值（``artifacts.demo-report.md`` 先整段再逐级，容忍键里带点）。"""
    if not isinstance(data, dict):
        return None
    if dotted in data:
        return data[dotted]
    current: Any = data
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if not isinstance(current, dict):
            return None
        rest = ".".join(parts[i:])
        if rest in current:  # 剩余整段作为键（文件名等带点的键）
            return current[rest]
        current = current.get(part)
    return current


def _check_no_error(check: dict, observation: dict, checkpoint: dict) -> str | None:
    error = observation.get("error")
    return f"步骤报错：{error}" if error else None


def _check_exit_code(check: dict, observation: dict, checkpoint: dict) -> str | None:
    expected = int(check.get("value", 0))
    actual = observation.get("exit_code")
    if actual is None:
        return f"observation 缺少 exit_code（期望 {expected}）"
    if int(actual) != expected:
        return f"exit_code {actual} != 期望 {expected}"
    return None


def _check_artifact_exists(check: dict, observation: dict, checkpoint: dict) -> str | None:
    key = str(check.get("key") or "")
    value = _dig(checkpoint, key)
    if value is None or value == "" or value == [] or value == {}:
        return f"产物缺失：checkpoint 中 {key!r} 为空"
    return None


def _check_schema_valid(check: dict, observation: dict, checkpoint: dict) -> str | None:
    field = str(check.get("field") or "")
    data = _dig(observation, field) if field else observation
    if not isinstance(data, dict):
        return f"字段 {field or '<observation>'} 不是对象（实际 {type(data).__name__}）"
    required = check.get("required_keys") or []
    missing = [k for k in required if k not in data]
    if missing:
        return f"字段 {field or '<observation>'} 缺少必需键：{missing}"
    return None


def _check_metric(check: dict, observation: dict, checkpoint: dict) -> str | None:
    name = str(check.get("name") or "")
    op = str(check.get("op") or ">=")
    target = check.get("value")
    if op not in _OPS or target is None:
        return f"metric 检查配置非法（op={op!r} value={target!r}）"
    metrics = observation.get("metrics")
    value = metrics.get(name) if isinstance(metrics, dict) else None
    if value is None:
        return f"指标 {name} 未在 observation.metrics 中给出（期望 {op} {target}）"
    try:
        ok = _OPS[op](float(value), float(target))
    except (TypeError, ValueError):
        return f"指标 {name} 值 {value!r} 不是数字"
    return None if ok else f"指标 {name} = {value}，不满足 {op} {target}"


def _check_min_count(check: dict, observation: dict, checkpoint: dict) -> str | None:
    field = str(check.get("field") or "")
    minimum = int(check.get("value", 1))
    value = _dig(observation, field)
    if isinstance(value, int | float):
        count = int(value)
    elif isinstance(value, list | dict | str):
        count = len(value)
    else:
        return f"字段 {field} 缺失或不可计数（期望数量 ≥ {minimum}）"
    if count < minimum:
        return f"字段 {field} 数量 {count} < 最低要求 {minimum}"
    return None


_CHECKERS: dict[str, Callable[[dict, dict, dict], str | None]] = {
    "no_error": _check_no_error,
    "exit_code": _check_exit_code,
    "artifact_exists": _check_artifact_exists,
    "schema_valid": _check_schema_valid,
    "metric": _check_metric,
    "min_count": _check_min_count,
}


def validate_checks(raw: Any) -> list[dict[str, Any]]:
    """校验计划里的 checks 声明；非法抛 ValueError（Navigator 计划校验用）。"""
    if not isinstance(raw, list):
        raise ValueError("checks must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"check {i} is not an object")
        kind = item.get("kind")
        if kind not in CHECK_KINDS:
            raise ValueError(f"check {i} has unknown kind: {kind!r}")
        out.append(item)
    return out


def run_deterministic_checks(
    checks: list[dict[str, Any]],
    *,
    observation: dict[str, Any] | None,
    checkpoint: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """跑全部确定性检查。

    返回 (verdict, pending_rubrics)：
    - 任一确定性检查失败 → (fail verdict, [])，reason 指明哪条检查、期望与实际；
    - 全部通过且无 llm_rubric → (pass verdict, [])；
    - 全部通过但有 llm_rubric → (None, rubrics)，由 Sextant 走 LLM 判定。
    """
    obs = observation if isinstance(observation, dict) else {}
    cp = checkpoint if isinstance(checkpoint, dict) else {}
    rubrics: list[dict[str, Any]] = []
    passed_kinds: list[str] = []
    for check in checks:
        kind = str(check.get("kind") or "")
        if kind == "llm_rubric":
            rubrics.append(check)
            continue
        checker = _CHECKERS.get(kind)
        if checker is None:
            return {"passed": False, "reason": f"未知检查类型：{kind!r}"}, []
        error = checker(check, obs, cp)
        if error:
            return {"passed": False, "reason": f"[{kind}] {error}"}, []
        passed_kinds.append(kind)
    if rubrics:
        return None, rubrics
    summary = "、".join(passed_kinds) or "无"
    return {"passed": True, "reason": f"确定性检查全部通过（{summary}）"}, []
