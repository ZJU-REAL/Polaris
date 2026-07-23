"""单篇文献相关性打分（LLM stage=relevance）。

voyage 的 wiki.score_relevance 动作与手动添加端点共用：按项目 definition 组
context（稀疏容忍）→ 调 LLM → 解析 → 写成员行 relevance_score/scored_at 与论文 tldr。
本模块不改成员行 status、不 commit——状态转移与落库时机由调用方决定。
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.base import utcnow
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.project import Project

logger = logging.getLogger(__name__)

RELEVANCE_SYSTEM_PROMPT = """\
你是文献相关性评审，对照研究方向定义评估一篇论文（只看标题与摘要）。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"score": 0 到 1 之间的小数, "reason": "简要理由", "tldr": "一句话中文总结"}
"""


@dataclass
class RelevanceResult:
    score: float
    reason: str
    tldr: str


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


def build_relevance_context(project: Project) -> str:
    """按项目 definition 组打分 context；rubric / questions 缺失时只用 statement。"""
    definition = project.definition if isinstance(project.definition, dict) else {}
    rubric = definition.get("rubric") or []
    questions = definition.get("questions") or []
    statement = definition.get("statement") or project.name
    lines = [f"研究方向：{statement}"]
    if rubric:
        lines.append(f"评分标准（rubric）：{json.dumps(rubric, ensure_ascii=False)}")
    if questions:
        lines.append(f"研究问题：{json.dumps(questions, ensure_ascii=False)}")
    return "\n".join(lines)


async def score_paper_relevance(
    paper: Paper,
    membership: LibraryPaper,
    *,
    context_text: str,
    llm: LLMRouter | None = None,
    extra_guidance: str = "",
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> RelevanceResult:
    """对一篇论文打相关性分：写成员行 relevance_score/scored_at 与论文 tldr。

    不改成员行 status、不 commit（由调用方决定状态转移与落库时机）。
    extra_guidance：追加到 system prompt 的补充指引（voyage 技能注入点用）。
    """
    llm = llm or get_llm_router()
    user_prompt = f"{context_text}\n标题：{paper.title}\n摘要：{paper.abstract or '（无摘要）'}"
    result = await llm.complete(
        "relevance",
        [
            Message(role="system", content=RELEVANCE_SYSTEM_PROMPT + extra_guidance),
            Message(role="user", content=user_prompt),
        ],
        user_id=user_id,
        project_id=project_id,
        library_id=membership.library_id,  # 打分是库侧判断，记方向库账（P6）
        voyage_id=voyage_id,
    )
    data = _extract_json(result.content)
    score = min(1.0, max(0.0, float(data["score"])))
    membership.relevance_score = score
    paper.tldr = str(data.get("tldr") or "") or paper.tldr
    membership.scored_at = utcnow()
    return RelevanceResult(score=score, reason=str(data.get("reason") or ""), tldr=paper.tldr or "")


async def score_added_paper_best_effort(
    session: AsyncSession,
    paper: Paper,
    membership: LibraryPaper,
    project: Project,
    *,
    user_id: uuid.UUID | None = None,
) -> None:
    """手动添加后的顺带打分（best-effort）：成功则 commit 分数，失败只记 warning。

    不改成员行 status（手动添加 = 人工纳入，分低也保持 included）；LLM 失败/超时时
    回滚未落库的字段改动，论文本身照常保留。
    """
    try:
        await score_paper_relevance(
            paper,
            membership,
            context_text=build_relevance_context(project),
            user_id=user_id,
            project_id=project.id,
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — 顺带增值，失败不影响添加本身
        logger.warning("best-effort relevance scoring failed for paper %s", paper.id, exc_info=True)
        await session.rollback()
