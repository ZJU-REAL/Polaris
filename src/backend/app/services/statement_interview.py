"""用一轮结构化访谈问出文献库的研究方向描述（不 import fastapi）。

为什么要访谈而不是让人自己写：statement 同时决定「粗排挑哪些论文」和「LLM 怎么打分」，
写得含糊后果很实在——生产上 12 个库的 statement 全是 3~31 字符的标签，其中一个只有
三个字母（``CUA``）。拿缩写去嵌入，排在最前的是超图小样本学习和医学影像论文，而真正
的 GUI agent 论文排在几百名之后。换成一段正常描述后，那些无关论文掉到 200~600 名开外。

**输出为英文**：statement 有两处用途——向量嵌入与 LLM 打分提示。后者中英皆可，前者对
语言敏感，而语料是英文 arXiv 摘要（实测中文 statement 会把中文相关的论文整体抬高，
把「中文歇后语」这类论文顶进前十）。

访谈是**无状态**的：前端每次把已答内容全量带回来，服务端据此决定下一题或收尾。这样
刷新页面、换设备都不会丢，也省掉一张会话表。
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# 访谈骨架固定，内容由 LLM 按主题现生成：骨架保证每次体验一致、LLM 不会跑题，
# 生成的选项保证问题贴着这个方向而不是泛泛而问。
STAGES: tuple[tuple[str, str, str], ...] = (
    (
        "question",
        "研究问题",
        "这个方向想回答的核心问题是什么",
    ),
    (
        "subject",
        "研究对象",
        "研究的是哪一类系统、模型或数据",
    ),
    (
        "subproblems",
        "研究子问题",
        "方向内部具体关心哪几个子问题",
    ),
    (
        "methods",
        "关心的方法类型",
        "偏重哪类方法、以及明确不想要什么",
    ),
)
STAGE_KEYS = tuple(k for k, _, _ in STAGES)
OPTIONS_PER_QUESTION = 3

_QUESTION_PROMPT = """\
你是科研文献库的访谈助手（POLARIS_STATEMENT_INTERVIEW）。你要通过几个问题，帮用户把\
一个模糊的研究方向名称，变成一段能用于自动筛选论文的方向描述。

现在请针对「{stage_title}」这一环节提问：{stage_hint}。

要求：
- 问题要**贴着这个具体方向**问，不要问放之四海皆准的空话；
- 给出 {n} 个候选答案，每个都是一个具体、可勾选的短句（不超过 30 个字），彼此不重叠；
- 候选答案要覆盖这个方向下最可能的几种情况，让用户多选；
- 已经答过的内容不要重复问。

只输出一个 JSON 对象，不要解释、不要代码块围栏：
{{"question": "问题正文", "options": ["选项1", "选项2", "选项3"]}}
"""

_STATEMENT_PROMPT = """\
你是科研文献库的方向描述撰写助手（POLARIS_STATEMENT_WRITER）。根据下面的访谈记录，\
写一段**英文**的研究方向描述，它会被用于两件事：计算论文的向量相似度、以及让大模型\
给论文打相关性分。

要求：
- **只输出英文**，不要中文，不要标题，不要列表，不要代码块围栏；
- 一段连贯的散文，3~5 句，120~200 个词；
- 必须写清四件事：研究什么问题、研究对象是什么、包含哪些子问题、偏重哪类方法\
（若访谈里提到不要什么，也写进去）；
- 多用该领域的**具体术语**（模型名、任务名、评测集名、方法名），它们是向量匹配的锚点；
- 不要出现「本文献库」「我们」这类自指，直接描述方向本身。
"""


@dataclass
class Answer:
    """用户对某一环节的回答：勾选项 + 自己补充的内容。"""

    stage: str
    selected: list[str]
    custom: str = ""

    def as_text(self) -> str:
        parts = [*self.selected]
        if self.custom.strip():
            parts.append(self.custom.strip())
        return "；".join(parts) or "（跳过）"


@dataclass
class Question:
    stage: str
    title: str
    hint: str
    question: str
    options: list[str]


def _stage_meta(stage: str) -> tuple[str, str]:
    for key, title, hint in STAGES:
        if key == stage:
            return title, hint
    return stage, ""


def next_stage(answers: list[Answer]) -> str | None:
    """还没答的第一个环节；全部答完返回 None。"""
    answered = {a.stage for a in answers}
    for key in STAGE_KEYS:
        if key not in answered:
            return key
    return None


def _transcript(topic: str, answers: list[Answer]) -> str:
    lines = [f"研究方向名称：{topic.strip() or '（未填写）'}"]
    for a in answers:
        title, _ = _stage_meta(a.stage)
        lines.append(f"{title}：{a.as_text()}")
    return "\n".join(lines)


def _json_object(content: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象；抠不出返回 None（调用方走兜底）。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _fallback_question(stage: str) -> Question:
    """LLM 不可用时也要能往下走：给一个没有选项的开放题，用户自己填。"""
    title, hint = _stage_meta(stage)
    return Question(stage=stage, title=title, hint=hint, question=f"{hint}？", options=[])


async def ask(
    *,
    topic: str,
    answers: list[Answer],
    llm: LLMRouter,
    user_id: Any = None,
) -> Question | None:
    """生成下一题；全部答完返回 None。

    模型挂了不抛错——退回一个开放题让用户自己写，访谈不能因为 LLM 不可用就走不下去。
    """
    stage = next_stage(answers)
    if stage is None:
        return None
    title, hint = _stage_meta(stage)
    system = _QUESTION_PROMPT.format(stage_title=title, stage_hint=hint, n=OPTIONS_PER_QUESTION)
    try:
        result = await llm.complete(
            "extract",
            [
                Message(role="system", content=system),
                Message(role="user", content=_transcript(topic, answers)),
            ],
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001 — 访谈不能被 LLM 故障卡死
        logger.warning("statement interview: question generation failed", exc_info=True)
        return _fallback_question(stage)

    data = _json_object(result.content or "")
    if not data:
        return _fallback_question(stage)
    options = [
        str(o).strip()
        for o in (data.get("options") or [])
        if isinstance(o, str | int | float) and str(o).strip()
    ][:OPTIONS_PER_QUESTION]
    question = str(data.get("question") or "").strip() or f"{hint}？"
    return Question(stage=stage, title=title, hint=hint, question=question, options=options)


async def compose(
    *,
    topic: str,
    answers: list[Answer],
    llm: LLMRouter,
    user_id: Any = None,
) -> str:
    """把访谈记录写成一段英文方向描述。失败时返回空串，由调用方决定怎么提示。"""
    try:
        result = await llm.complete(
            "extract",
            [
                Message(role="system", content=_STATEMENT_PROMPT),
                Message(role="user", content=_transcript(topic, answers)),
            ],
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("statement interview: compose failed", exc_info=True)
        return ""
    text = (result.content or "").strip()
    # 模型偶尔会裹一层代码块围栏
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()
