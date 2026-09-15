"""任务运行时的指引注入：读 checkpoint 快照、渲染进 system prompt。

前身是 SkillSet（读 ``checkpoint["skills"]``）。用户技能功能整体移除后
（插件覆盖了那块能力），这条通道只剩一个生产者——跨学科工作流
（services/interdisciplinary_workflows）把它的工作流与逐环节指引写进快照。

保留通道而不是一并删掉：跨学科能力和技能是两回事，只是当初为了省事让前者
伪装成一条技能骑在后者的快照上。现在它是这里唯一的住户，名字也就该照实写。

快照结构 ``{注入点: [{slug, name, kind, version, body, config, steps}, ...]}``。
本模块只读快照、不碰 DB——断点恢复与审计回放天然自包含。
"""

from typing import Any

#: 参与 system prompt 注入的条目类型；workflow 由 navigator 单独读取
_GUIDANCE_KINDS = ("guidance", "rubric")
#: 单注入点渲染上限（约 6K token），超出截断并留标记
_TARGET_BUDGET_CHARS = 24000

_HEADER = (
    "\n\n【工作流指引】以下内容来自本课题启用的跨学科工作流，作为本环节的补充判断"
    "标准；它们不改变上文规定的输出格式：\n"
)

#: 旧 checkpoint 里这份快照存在 "skills" 下。进行中的任务还带着旧键，
#: 读两处才不会让它们在中途丢掉指引——而那种丢失不报错，只是这一步之后
#: 判断标准悄悄变了。
_SNAPSHOT_KEYS = ("guidance", "skills")


def _entries(checkpoint: dict[str, Any] | None, target: str) -> list[dict[str, Any]]:
    data = checkpoint or {}
    for key in _SNAPSHOT_KEYS:
        snapshot = data.get(key)
        if not isinstance(snapshot, dict):
            continue
        entries = snapshot.get(target)
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
    return []


def _render_entry(entry: dict[str, Any]) -> str:
    header = f"### {entry.get('name')}（{entry.get('slug')} v{entry.get('version')}）"
    lines = [header, str(entry.get("body") or "").strip()]
    config = entry.get("config")
    if isinstance(config, dict) and config:
        pairs = ", ".join(f"{k}={v}" for k, v in config.items())
        lines.append(f"（本课题配置：{pairs}）")
    return "\n".join(lines)


def workflow_guidance(checkpoint: dict[str, Any] | None, *targets: str) -> str:
    """指定注入点上的指引文本；没有则返回空串。

    返回值以换行开头，调用方直接 ``system_prompt + workflow_guidance(...)`` 即可。
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
        text = text[:_TARGET_BUDGET_CHARS] + "\n（指引内容超长，已截断）"
    return _HEADER + text


def workflows(checkpoint: dict[str, Any] | None) -> list[dict[str, Any]]:
    """navigator.free_plan 上的工作流条目（自由规划的计划模板）。"""
    return [
        e for e in _entries(checkpoint, "navigator.free_plan") if e.get("kind") == "workflow"
    ]
