"""内联 AI 写作辅助（选中润色 / 按指示改写 / 光标续写）。

与整篇 AI 起草（voyage）不同：这是编辑器里的一问一答，同步 SSE 流式返回，
作者看过结果再决定是否应用，所以只做 prompt 级约束 + 流结束后的引用/图表
静态检查（以 warnings 事件提示，不强制重写）。路由见 api/manuscripts.py §6。
"""

import re
from pathlib import PurePosixPath
from typing import Any

from app.core.llm.base import Message
from app.models.manuscript import Manuscript

ASSIST_MODES = ("polish", "rewrite", "continue")

# 输入长度上限（超出直接截断，避免把整篇稿件塞进 prompt）
MAX_TEXT_CHARS = 12_000
MAX_CONTEXT_CHARS = 3_000
MAX_INSTRUCTION_CHARS = 2_000
# 事实包注入上限
_MAX_CITATIONS = 60
_MAX_FIGURES = 20
_MAX_METRICS = 30

_CITE_RE = re.compile(r"\\cite[tp]?\*?(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^{}]*)\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}")

SYSTEM_PROMPT = """\
你是 Polaris 的论文写作助手（POLARIS_WRITING_ASSIST），在作者编辑 LaTeX 稿件时
润色/改写/续写一小段文字。直接输出可替换回稿件的 LaTeX 文本（不要 Markdown
代码块、不要解释说明、不要 \\section 标题行）。
硬约束：
- \\cite{key} 只准使用下方可引用文献列表中的 bibkey；没有合适的就不加引用；
- 不得编造实验数字；数值只能来自下方实验指标或原文中已有的数字；
- \\includegraphics 只准引用可用图表列表中的 fig_id（路径 figures/<fig_id>.pdf）；
- 除非任务本身要求，保持原文中已有的 LaTeX 命令、引用与数字不变。
"""


def _clamp(text: str, limit: int) -> str:
    return text[:limit]


def _fact_pack_brief(fact_pack: dict[str, Any] | None) -> str:
    """事实包速览（引文 bibkey / 指标 / 图表），供 prompt 注入。"""
    fp = fact_pack or {}
    lines: list[str] = []
    citations = (fp.get("citations") or [])[:_MAX_CITATIONS]
    if citations:
        lines.append("可引用文献（bibkey：标题）：")
        for c in citations:
            year = f"（{c.get('year')}）" if c.get("year") else ""
            lines.append(f"- {c.get('bibkey')}: {c.get('title', '')}{year}")
    else:
        lines.append("可引用文献：无（不要使用 \\cite）")
    metrics = (fp.get("metrics") or [])[:_MAX_METRICS]
    if metrics:
        lines.append("实验指标（名称 = 最优值）：")
        for m in metrics:
            lines.append(f"- {m.get('name')} = {m.get('best')}")
    figures = (fp.get("figures") or [])[:_MAX_FIGURES]
    if figures:
        lines.append("可用图表（fig_id：图注）：")
        for f in figures:
            lines.append(f"- {f.get('fig_id')}: {f.get('caption') or ''}")
    return "\n".join(lines)


def build_assist_messages(
    manuscript: Manuscript,
    *,
    mode: str,
    text: str = "",
    instruction: str = "",
    before: str = "",
    after: str = "",
) -> list[Message]:
    """组装内联辅助的 LLM 消息。mode ∈ ASSIST_MODES，入参已在 schema 层校验。"""
    text = _clamp(text, MAX_TEXT_CHARS)
    instruction = _clamp(instruction, MAX_INSTRUCTION_CHARS)
    before = _clamp(before, MAX_CONTEXT_CHARS)
    after = _clamp(after, MAX_CONTEXT_CHARS)

    parts: list[str] = [f"论文标题：{manuscript.title}", "", _fact_pack_brief(manuscript.fact_pack)]
    if before:
        parts += ["", "选区/光标前文（仅供衔接参考，不要重复输出）：", before]
    if after:
        parts += ["", "选区/光标后文（仅供衔接参考，不要重复输出）：", after]

    if mode == "polish":
        parts += [
            "",
            "任务：润色下面这段文字——改善行文、语法与逻辑衔接，"
            "保持原意，引用与数字一律不变。输出润色后的完整段落。",
        ]
        if instruction:
            parts += [f"作者补充要求：{instruction}"]
        parts += ["", "待润色文字：", text]
    elif mode == "rewrite":
        parts += [
            "",
            f"任务：按下面的要求改写这段文字。要求：{instruction}",
            "",
            "待改写文字：",
            text,
        ]
    else:  # continue
        parts += [
            "",
            "任务：在前文之后续写一段自然衔接的内容（不超过约 200 个英文词），"
            "只输出新增的续写部分。",
        ]
        if instruction:
            parts += [f"作者补充要求：{instruction}"]

    return [Message("system", SYSTEM_PROMPT), Message("user", "\n".join(parts))]


def scan_result_warnings(fact_pack: dict[str, Any] | None, result: str) -> list[str]:
    """流结束后的静态检查：越界 \\cite / \\includegraphics 提示（不阻断，人来定）。"""
    fp = fact_pack or {}
    allowed_keys = {str(c.get("bibkey")) for c in fp.get("citations") or [] if c.get("bibkey")}
    fig_ids = {str(f.get("fig_id")) for f in fp.get("figures") or [] if f.get("fig_id")}
    warnings: list[str] = []
    for match in _CITE_RE.finditer(result):
        for key in match.group(1).split(","):
            key = key.strip()
            if key and key not in allowed_keys:
                warnings.append(f"引用 \\cite{{{key}}} 不在事实包文献里，可能是编造的")
    for match in _GRAPHICS_RE.finditer(result):
        stem = PurePosixPath(match.group(1).strip()).stem
        if stem not in fig_ids:
            warnings.append(f"图表 {match.group(1).strip()} 不在事实包图表里，编译会缺文件")
    return warnings
