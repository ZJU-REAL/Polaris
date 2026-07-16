"""SkillSet：读取 voyage checkpoint 的技能快照并渲染注入文本（docs/skill-system.md §3.2）。

快照由 engine 在首次驱动时写入 checkpoint["skills"]（services.skills.snapshot_for_project），
结构：{target: [{slug, name, kind, version, body, config, personas, steps}, ...]}。
本模块只读快照、不碰 DB——断点恢复与审计回放天然自包含。
"""

import json
from typing import Any

# 参与 system prompt 注入的技能类型；persona/workflow 由专门的消费方读取
_GUIDANCE_KINDS = ("guidance", "rubric")
# 单注入点渲染上限（约 6K token），超出截断并留标记
_TARGET_BUDGET_CHARS = 24000

_HEADER = (
    "\n\n【项目技能指引】以下技能由项目成员启用，作为本环节的补充判断标准；"
    "它们不改变上文规定的输出格式：\n"
)


def _entries(checkpoint: dict[str, Any] | None, target: str) -> list[dict[str, Any]]:
    snapshot = (checkpoint or {}).get("skills")
    if not isinstance(snapshot, dict):
        return []
    entries = snapshot.get(target)
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _render_entry(entry: dict[str, Any]) -> str:
    header = f"### 技能：{entry.get('name')}（{entry.get('slug')} v{entry.get('version')}）"
    lines = [header, str(entry.get("body") or "").strip()]
    config = entry.get("config")
    if isinstance(config, dict) and config:
        pairs = ", ".join(f"{k}={v}" for k, v in config.items())
        lines.append(f"（本项目配置：{pairs}）")
    return "\n".join(lines)


def skill_guidance(checkpoint: dict[str, Any] | None, *targets: str) -> str:
    """指定注入点上 guidance/rubric 技能的拼接文本；无技能时返回空串。

    返回值以换行开头，调用方直接 ``system_prompt + skill_guidance(...)`` 即可。
    """
    blocks = [
        _render_entry(e)
        for target in targets
        for e in _entries(checkpoint, target)
        if e.get("kind") in _GUIDANCE_KINDS and e.get("body")
    ]
    if not blocks:
        return ""
    text = "\n\n".join(blocks)
    if len(text) > _TARGET_BUDGET_CHARS:
        text = text[:_TARGET_BUDGET_CHARS] + "\n（技能内容超长，已截断）"
    return _HEADER + text


def skill_personas(checkpoint: dict[str, Any] | None, target: str) -> list[dict[str, Any]] | None:
    """persona 技能定义的人设列表；无则 None（调用方回退内置默认）。"""
    personas: list[dict[str, Any]] = []
    for entry in _entries(checkpoint, target):
        if entry.get("kind") != "persona":
            continue
        personas.extend(p for p in entry.get("personas") or [] if isinstance(p, dict))
    return personas or None


def skill_workflows(checkpoint: dict[str, Any] | None) -> list[dict[str, Any]]:
    """navigator.free_plan 上启用的 workflow 技能（自由规划的计划模板，S3 消费）。"""
    return [e for e in _entries(checkpoint, "navigator.free_plan") if e.get("kind") == "workflow"]


# ---- output_contract：产出约束的确定性校验（Sextant 在 LLM 判定前先跑）----


def skill_output_contract(checkpoint: dict[str, Any] | None, target: str) -> dict[str, Any] | None:
    """注入点上第一个声明了 output_contract 的技能约定；无则 None。"""
    for entry in _entries(checkpoint, target):
        contract = entry.get("output_contract")
        if isinstance(contract, dict) and contract:
            return contract
    return None


def _json_type_ok(value: Any, expected: str) -> bool:
    match expected:
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "null":
            return value is None
    return True  # 未知类型不校验


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """极简 JSON Schema 子集校验（type/required/properties/items/enum），返回首个错误。"""
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _json_type_ok(value, expected_type):
        return f"{path} 应为 {expected_type}，实际是 {type(value).__name__}"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        return f"{path} 取值不在 enum {enum} 内"
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                return f"{path} 缺少必填字段 {key!r}"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key not in value or not isinstance(sub, dict):
                    continue
                if error := _validate_schema(value[key], sub, f"{path}.{key}"):
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            if error := _validate_schema(item, schema["items"], f"{path}[{i}]"):
                return error
    return None


def _extract_json_value(content: str) -> Any:
    """容忍代码块围栏/前后杂讯：截取首个 '{'/'[' 到对应末尾解析。"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (content.find("{"), content.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON value found")
    start = min(starts)
    end = content.rfind("}" if content[start] == "{" else "]")
    if end <= start:
        raise ValueError("no JSON value found")
    return json.loads(content[start : end + 1])


def check_output_contract(contract: dict[str, Any], content: str) -> str | None:
    """按技能 output_contract 校验产出文本；返回错误描述，通过返回 None。

    仅 format=json 做确定性校验（解析 + 可选 json_schema 子集）；其他格式放行。
    """
    if str(contract.get("format") or "").lower() != "json":
        return None
    try:
        value = _extract_json_value(content)
    except (ValueError, json.JSONDecodeError) as e:
        return f"产出不是合法 JSON（{e}）"
    schema = contract.get("json_schema")
    if isinstance(schema, dict) and schema:
        return _validate_schema(value, schema, "$")
    return None
