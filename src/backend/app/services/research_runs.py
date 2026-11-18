"""研究：一个方向从输入走到「等用户选定想法」（不 import fastapi）。

一次研究 = ``research_runs`` 的一行。它不自己干活，只负责把三段已有的东西接起来：

    plan（确定范围）→ survey（检索文献，wiki_bootstrap 任务）
      → hypotheses（寻找假设，discovery 任务）→ choose（等用户选定想法）

**谁在推进**：:func:`advance`，也是唯一改 ``phase`` / ``status`` 的地方。它每次都从头
看一遍「这一行 + 当前阶段那条任务的状态 + 库里的事实」，由纯函数 :func:`decide` 给出
该到哪一步，所以调多少次、什么时候调结果都一样：

1. 每条任务跑完（或停下）时，worker/tasks.py 的 ``_drive_voyage`` 在收尾处调一次；
2. 创建、重试这些接口入队 ``research_advance``；
3. 定时任务每分钟对所有没结束的研究调一次——引擎重启、用户在任务页直接取消或
   恢复了那条任务，都靠它接回来。

状态不是记下来的，是每次推出来的（只有用户点的「停止」是记下来的）：比如检索失败后
用户去任务页手动恢复了那条任务，下一次 advance 看到任务在跑，研究就回到「运行中」。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.failures import (
    auto_retry_attempt,
    auto_retry_due_at,
    classify_failure,
    clear_auto_retry,
)
from app.core.llm.base import Message
from app.models.base import utcnow
from app.models.hypothesis import HypothesisNode
from app.models.idea import Idea
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.project import Project
from app.models.research_run import ResearchRun
from app.models.ssh_credential import ConnectionCredential
from app.models.voyage import IN_FLIGHT_STATUSES, TERMINAL_STATUSES, VoyageRun, VoyageStep
from app.schemas.ingest import IngestKnobs
from app.schemas.project import ProjectCreate
from app.schemas.research import (
    BriefDraft,
    Capabilities,
    EvidenceCounts,
    HypothesesRead,
    LastErrorRead,
    ResearchAttention,
    ResearchBrief,
    ResearchBriefInput,
    ResearchCounts,
    ResearchIdea,
    ResearchRunDetail,
    ResearchRunRead,
    SourceResult,
    SurveyProgress,
    SurveyRead,
    TopPaper,
    UsageRead,
)
from app.schemas.voyage import VoyageCreate, VoyageMessageRead
from app.services import ingest as ingest_service
from app.services import libraries as libraries_service
from app.services import projects as projects_service
from app.services import voyage_messages as messages_service
from app.services import voyages as voyages_service
from app.services.papers import PAPER_STATUS_GROUPS

logger = logging.getLogger(__name__)

Enqueue = Callable[..., Awaitable[Any]]

BRIEF_STAGE = "research_brief"
# 确定范围最多试这么多次（每分钟的定时任务各试一次），之后停下来等用户点重试
MAX_PLAN_ATTEMPTS = 3
# 寻找假设默认探索几轮（每轮最多 4 个新假设）
DISCOVERY_ROUNDS = 4
# 没带幂等键时：同一个人 60 秒内提交同样的方向，算同一次（双击、重发）
DUPLICATE_WINDOW_SECONDS = 60
# 「相关的太少」之后放宽重新检索用的两个值
BROADEN_THRESHOLD = 0.5
BROADEN_MONTHS = 60
# 课题的临时名字：方向原文的前这么多个字（确定范围后换成简报里的标题）
PROVISIONAL_NAME_CHARS = 40
MONTHS_BACK_CHOICES = (12, 36, 60)

MIN_CORPUS_PAPERS = voyages_service.MIN_CORPUS_PAPERS
# 密钥不对 / 没配模型的错误码：引擎按异常类型归的（AUTH…）和模型错误自带的（LLM_AUTH…）
AUTH_CODES = frozenset({"AUTH", "NOT_CONFIGURED", "LLM_AUTH", "LLM_NOT_CONFIGURED"})

BRIEF_SYSTEM_PROMPT = """\
POLARIS_RESEARCH_BRIEF
你为一次文献调研写检索简报。用户给出一个研究方向（可能是中文，也可能是一句问话）。
输出一个 JSON：
- title：课题名，10–30 个字，用研究方向本身的语言写；写成名词短语，不要用问句。
- language：研究方向是中文写的填 zh，否则填 en。
- statement：3–5 句，说清研究的问题、对象、涉及的方法，以及什么样的论文算相关。
  用 language 的语言写。
- questions：3–5 个具体的研究问题，用 language 的语言写。
- include_keywords：4–8 个**英文**检索短语。文献来源都按英文检索，研究方向是中文也
  必须给英文。每个 2–5 个词，用论文标题和摘要里会出现的术语；既要有覆盖方向核心的
  宽短语，也要有指向具体方法的窄短语。
- exclude_keywords：容易混进来但明显不相关的英文短语；没有就留空。
- arxiv_categories：相关的 arXiv 分类号（如 cs.CL、cs.IR、stat.ML）；方向不在 arXiv
  的覆盖范围内就留空。
- discipline：从「已安装的学科包」里选最贴切的一个；都不贴切填 null。
- sources：从「可选来源」里选适合这个方向的 2–4 个。
- in_scope / out_of_scope：各 2–5 条，什么算在范围内、什么不算。
- months_back：回溯多久的文献，只能是 12、36 或 60。发展很快的方向选 12 或 36，
  需要经典工作打底的选 60。
"""


class ResearchError(Exception):
    """接口层按 ``code`` 回 409。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- 事实与决定（纯函数部分） ----


@dataclass(frozen=True, slots=True)
class ChildFacts:
    """当前阶段那条任务的状态里，决定下一步要看的那几项。"""

    status: str
    retry_pending: bool = False  # 因临时故障停着，引擎已经排了下一次自动重试
    error_code: str | None = None
    error_message: str = ""
    error_transient: bool = False
    failed_action: str | None = None  # 停在哪一步
    search_stats: dict[str, Any] | None = None  # 文献检索那一步留下的统计


@dataclass(frozen=True, slots=True)
class Facts:
    plan_attempts: int = 0
    plan_error: dict[str, Any] | None = None
    child: ChildFacts | None = None
    papers_found: int = 0  # 文献库里的全部条目（含候选和被排除的）
    papers_kept: int = 0  # 留在库里的
    papers_unscreened: int = 0  # 还没判断过相关度的候选
    papers_indexed: int = 0  # 其中能被语义检索到的
    ideas: int = 0
    picked: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    #: none：只更新阶段和状态；plan：去做确定范围；start_hypotheses：建寻找假设的任务
    action: str
    phase: str
    status: str
    attention: dict[str, Any] | None = field(default=None)


def _attention(
    code: str, action: str, detail: str = "", counts: dict[str, int] | None = None
) -> dict[str, Any]:
    return {"code": code, "detail": detail, "counts": counts or {}, "action": action}


def _needs(phase: str, attention: dict[str, Any]) -> Decision:
    return Decision("none", phase, "needs_attention", attention)


def _plan_attention(error: dict[str, Any] | None) -> dict[str, Any]:
    error = error or {}
    code = str(error.get("code") or "")
    if code in AUTH_CODES:
        return _attention("LLM_AUTH", "retry", str(error.get("message") or ""))
    if error.get("transient"):
        return _attention("LLM_UNREACHABLE", "retry", str(error.get("message") or ""))
    return _attention("PLAN_FAILED", "retry", str(error.get("message") or ""))


def _child_failure(phase: str, child: ChildFacts) -> dict[str, Any]:
    """任务停在出错状态、自动重试也用完了（或根本不该自动重试）时给出原因。"""
    if child.error_code in AUTH_CODES:
        return _attention("LLM_AUTH", "retry", child.error_message)
    if phase == "survey":
        stats = child.search_stats or {}
        fetched = int(stats.get("source_fetched", 0) or 0)
        # 停在检索那一步、是连不上这类故障、而且一篇都没拿到：是来源连不上，
        # 不是这一步本身有问题——文案不同，用户该查的是网络
        if (
            child.failed_action == "wiki.search_candidates"
            and child.error_transient
            and fetched == 0
        ):
            return _attention("SURVEY_SOURCES_UNREACHABLE", "retry", child.error_message)
    # 检索之后的每一步、以及寻找假设的每一步，往外连的都是模型服务（下载全文失败是
    # 逐篇降级的，不会让整步失败）。所以连不上、超时、限流这类临时故障在重试用完之后，
    # 原因就是模型服务连不上——比「这一步没有完成」有用：用户知道该等一等再试
    if child.error_transient and child.failed_action != "wiki.search_candidates":
        return _attention("LLM_UNREACHABLE", "retry", child.error_message)
    if phase == "survey":
        return _attention("SURVEY_FAILED", "retry", child.error_message)
    return _attention("HYPOTHESES_FAILED", "retry", child.error_message)


def classify_survey(stats: dict[str, Any] | None, facts: Facts) -> dict[str, Any] | None:
    """文献检索的任务跑完了：够不够往下走。返回 None = 够，否则是原因。

    任务「完成」不等于检索成功——每个来源都连不上、或者一篇相关的都没有，任务照样
    是完成状态。所以研究绝不因为任务完成就往下走，而是看库里实际有什么。
    """
    if facts.papers_indexed >= MIN_CORPUS_PAPERS:
        return None
    stats = stats or {}
    fetched = int(stats.get("source_fetched", 0) or 0)
    # 「找到多少篇」取这次检索拿到的数：判为不相关的论文会从库里移走，库里的总数
    # 在筛选之后只剩留下的那些
    counts = {"found": max(fetched, facts.papers_found), "relevant": facts.papers_kept}
    if facts.papers_kept < MIN_CORPUS_PAPERS and facts.papers_unscreened > 0:
        # 还有候选没筛过：不是「相关的太少」，是筛选这一步没做完。放宽范围帮不上忙
        return _attention(
            "SURVEY_FAILED",
            "retry",
            f"unscreened={facts.papers_unscreened} kept={facts.papers_kept}",
            counts=counts,
        )
    if fetched == 0 and facts.papers_kept < MIN_CORPUS_PAPERS:
        sources = [s for s in str(stats.get("source") or "").split(",") if s and s != "none"]
        errored = {
            str(e.get("source"))
            for e in (stats.get("source_errors") or [])
            if isinstance(e, dict)
        }
        if sources and all(s in errored for s in sources):
            return _attention("SURVEY_SOURCES_UNREACHABLE", "retry", counts=counts)
        return _attention("SURVEY_NO_RESULTS", "edit_scope", counts=counts)
    if facts.papers_kept < MIN_CORPUS_PAPERS:
        return _attention("SURVEY_TOO_FEW_RELEVANT", "broaden", counts=counts)
    # 留下的够了，但能被检索到的不够：全文和索引那几步没做成
    return _attention(
        "LIBRARY_TOO_SMALL",
        "retry",
        f"kept={facts.papers_kept} indexed={facts.papers_indexed}",
        counts={"papers": facts.papers_indexed, "required": MIN_CORPUS_PAPERS},
    )


def decide(phase: str, status: str, facts: Facts) -> Decision:
    """这一行现在该是什么阶段、什么状态，要不要做下一步（纯函数，见模块说明）。"""
    if status == "cancelled":  # 用户停止的：只有「重试」能把它拉回来
        return Decision("none", phase, "cancelled")

    if phase == "plan":
        fatal = bool((facts.plan_error or {}).get("fatal"))
        if fatal or facts.plan_attempts >= MAX_PLAN_ATTEMPTS:
            return _needs("plan", _plan_attention(facts.plan_error))
        return Decision("plan", "plan", "running")

    if phase in ("survey", "hypotheses"):
        child = facts.child
        if child is None:  # 任务记录被删了
            code = "SURVEY_FAILED" if phase == "survey" else "HYPOTHESES_FAILED"
            return _needs(phase, _attention(code, "retry", "这一步的任务记录已不存在"))
        if child.status == "cancelled":
            return Decision("none", phase, "cancelled")
        if child.status in ("paused_ask", "paused_gate"):
            return Decision("none", phase, "waiting_user")
        if child.status in IN_FLIGHT_STATUSES:
            return Decision("none", phase, "running")
        if child.status == "paused_error" and child.retry_pending:
            return Decision("none", phase, "running")  # 引擎自己会再试
        if child.status in ("paused_error", "failed"):
            return _needs(phase, _child_failure(phase, child))
        if child.status == "done":
            if phase == "survey":
                problem = classify_survey(child.search_stats, facts)
                if problem is not None:
                    return _needs("survey", problem)
                return Decision("start_hypotheses", "survey", "running")
            if facts.ideas < 1:
                return _needs(
                    "hypotheses", _attention("NO_HYPOTHESES_SURVIVED", "add_papers")
                )
            return Decision("none", "choose", "done" if facts.picked >= 1 else "waiting_user")
        return Decision("none", phase, "running")  # 不认识的状态：当它还在跑

    # choose：想法已经在那儿了，等用户选
    return Decision("none", "choose", "done" if facts.picked >= 1 else "waiting_user")


# ---- 读事实 ----


def _phase_state(run: ResearchRun, phase: str) -> dict[str, Any]:
    state = (run.phases or {}).get(phase)
    return state if isinstance(state, dict) else {}


def _phase_voyage_id(run: ResearchRun, phase: str) -> uuid.UUID | None:
    raw = _phase_state(run, phase).get("voyage_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def current_voyage_id(run: ResearchRun) -> uuid.UUID | None:
    """当前阶段的任务；choose 阶段没有在跑的任务，返回最近那条寻找假设的。"""
    if run.phase == "survey":
        return _phase_voyage_id(run, "survey")
    if run.phase in ("hypotheses", "choose"):
        return _phase_voyage_id(run, "hypotheses")
    return None


async def _load_voyage(session: AsyncSession, voyage_id: uuid.UUID | None) -> VoyageRun | None:
    if voyage_id is None:
        return None
    # 这条任务由引擎在别的会话里写；本会话里可能有旧值，强制重读
    return await session.get(VoyageRun, voyage_id, populate_existing=True)


async def _first_open_step(session: AsyncSession, voyage_id: uuid.UUID) -> VoyageStep | None:
    return (
        await session.execute(
            select(VoyageStep)
            .where(
                VoyageStep.run_id == voyage_id,
                VoyageStep.status.not_in(("passed", "obsolete")),
            )
            .order_by(VoyageStep.rank, VoyageStep.seq)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _child_facts(session: AsyncSession, voyage: VoyageRun | None) -> ChildFacts | None:
    if voyage is None:
        return None
    checkpoint = voyage.checkpoint if isinstance(voyage.checkpoint, dict) else {}
    error = checkpoint.get("last_error") if voyage.status == "paused_error" else None
    error = error if isinstance(error, dict) else {}
    failed_action = None
    if voyage.status in ("paused_error", "failed"):
        step = await _first_open_step(session, voyage.id)
        failed_action = step.action if step is not None else None
    stats = checkpoint.get("ingest_search_stats")
    return ChildFacts(
        status=voyage.status,
        retry_pending=auto_retry_due_at(checkpoint) is not None,
        error_code=str(error.get("code")) if error.get("code") else None,
        error_message=str(error.get("message") or ""),
        error_transient=bool(error.get("transient")),
        failed_action=failed_action,
        search_stats=stats if isinstance(stats, dict) else None,
    )


async def _idea_counts(session: AsyncSession, project_id: uuid.UUID) -> tuple[int, int]:
    rows = (
        await session.execute(
            select(Idea.status, func.count())
            .where(Idea.project_id == project_id, Idea.trashed_at.is_(None))
            .group_by(Idea.status)
        )
    ).all()
    total = sum(int(n) for status, n in rows if status != "rejected")
    picked = sum(int(n) for status, n in rows if status == "promoted")
    return total, picked


async def gather_facts(session: AsyncSession, run: ResearchRun) -> Facts:
    plan = _phase_state(run, "plan")
    child = None
    if run.phase in ("survey", "hypotheses"):
        child = await _child_facts(session, await _load_voyage(session, current_voyage_id(run)))
    found = kept = indexed = unscreened = 0
    if run.library_id is not None:
        counts = await ingest_service.library_paper_counts(session, run.library_id)
        found, kept = counts["total"], counts["library"]
        unscreened = counts["candidate"]
        indexed = await voyages_service.indexed_library_paper_count(session, run.library_id)
    ideas, picked = await _idea_counts(session, run.project_id)
    return Facts(
        plan_attempts=int(plan.get("attempts", 0) or 0),
        plan_error=plan.get("last_error") if isinstance(plan.get("last_error"), dict) else None,
        child=child,
        papers_found=found,
        papers_kept=kept,
        papers_unscreened=unscreened,
        papers_indexed=indexed,
        ideas=ideas,
        picked=picked,
    )


# ---- 简报 ----

_CJK_RE = re.compile(r"[㐀-鿿]")
_ARXIV_CATEGORY_RE = re.compile(r"^[a-z][a-z\-]*(\.[A-Za-z\-]+)?$")


def _clean_list(raw: Any, *, limit: int) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = " ".join(str(item or "").split())
        if text and text not in out:
            out.append(text)
    return out[:limit]


def selectable_sources() -> list[str]:
    from app.services.literature import sources as literature_sources

    return [sid for sid, _ in literature_sources.sources_with_capability("search")]


def _is_complete(overrides: dict[str, Any]) -> bool:
    """用户给的简报够不够直接用（不用再让模型推）。"""
    return bool(
        str(overrides.get("title") or "").strip()
        and str(overrides.get("statement") or "").strip()
        and (overrides.get("include_keywords") or overrides.get("arxiv_categories"))
    )


async def finalize_brief(
    session: AsyncSession, direction: str, draft: dict[str, Any], overrides: dict[str, Any]
) -> ResearchBrief:
    """模型推出来的（可能为空）+ 用户填的 → 一份校验过的简报。

    学科与来源在这里校验而不是做成 schema 枚举：不认识的值丢掉，来源丢空了用
    学科包声明的默认来源，再没有就用部署默认。关键词只留英文的——来源按英文检索，
    中文关键词什么都搜不到。
    """
    from app.services.discipline_packs import known_disciplines

    data = {**draft, **{k: v for k, v in overrides.items() if v is not None}}
    language = data.get("language")
    if language not in ("zh", "en"):
        language = "zh" if _CJK_RE.search(direction) else "en"
    title = " ".join(str(data.get("title") or "").split())[:80] or direction.strip()[:40]
    discipline = str(data.get("discipline") or "").strip() or None
    if discipline not in known_disciplines():
        discipline = None
    available = selectable_sources()
    sources = [s for s in _clean_list(data.get("sources"), limit=8) if s in available]
    if not sources:
        defaults = await libraries_service.default_library_sources(session, discipline)
        sources = [s for s in defaults if s in available]
    include = [
        k for k in _clean_list(data.get("include_keywords"), limit=8) if not _CJK_RE.search(k)
    ]
    categories = [
        c for c in _clean_list(data.get("arxiv_categories"), limit=8) if _ARXIV_CATEGORY_RE.match(c)
    ]
    if not include and not categories:
        raise ValueError("简报里没有可用的英文检索关键词，也没有 arXiv 分类")
    try:
        months = int(data.get("months_back") or 36)
    except (TypeError, ValueError):
        months = 36
    if overrides.get("months_back") is None:
        # 模型给的只认三档；用户自己填的原样用（1..120）
        months = min(MONTHS_BACK_CHOICES, key=lambda m: abs(m - months))
    knobs = {
        k: data[k]
        for k in ("max_papers", "compile_top_n", "relevance_threshold")
        if data.get(k) is not None
    }
    return ResearchBrief(
        title=title,
        language=language,
        statement=str(data.get("statement") or "").strip() or direction.strip(),
        questions=_clean_list(data.get("questions"), limit=6),
        include_keywords=include,
        exclude_keywords=_clean_list(data.get("exclude_keywords"), limit=8),
        arxiv_categories=categories,
        discipline=discipline,
        sources=sources,
        in_scope=_clean_list(data.get("in_scope"), limit=6),
        out_of_scope=_clean_list(data.get("out_of_scope"), limit=6),
        months_back=max(1, min(120, months)),
        **knobs,
    )


async def derive_brief(
    session: AsyncSession,
    direction: str,
    *,
    overrides: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> ResearchBrief:
    """一句研究方向 → 检索简报。用户给的简报已经够用时不调模型。"""
    from app.core.llm.router import get_llm_router
    from app.services.discipline_packs import known_disciplines

    overrides = dict(overrides or {})
    if _is_complete(overrides):
        return await finalize_brief(session, direction, {}, overrides)
    packs = "、".join(sorted(known_disciplines())) or "（无）"
    sources = "、".join(selectable_sources()) or "（无）"
    result = await get_llm_router().complete_json(
        BRIEF_STAGE,
        [
            Message(role="system", content=BRIEF_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"研究方向：{direction.strip()}\n\n已安装的学科包：{packs}\n可选来源：{sources}",
            ),
        ],
        schema=BriefDraft,
        user_id=user_id,
        project_id=project_id,
    )
    return await finalize_brief(session, direction, result.data.model_dump(), overrides)


def _library_fields(brief: ResearchBrief) -> dict[str, Any]:
    return {
        "statement": brief.statement,
        "questions": brief.questions,
        "in_scope": brief.in_scope,
        "out_of_scope": brief.out_of_scope,
        "keywords": {
            "include": brief.include_keywords,
            "exclude": brief.exclude_keywords,
            "arxiv_categories": brief.arxiv_categories,
            "sources": brief.sources,
        },
        # 研究建的库不进每日自动同步：用户只问了一次，不该每天替他花钱
        "cadence": "manual",
        "discipline": brief.discipline,
    }


# ---- 建各阶段的任务 ----


def _with_phase_voyage(
    run: ResearchRun, phase: str, voyage_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    """把某阶段的任务换成新的一条（旧的进 history），返回新的 phases。"""
    phases = copy.deepcopy(run.phases) if isinstance(run.phases, dict) else {}
    state = phases.get(phase) if isinstance(phases.get(phase), dict) else {}
    history = list(state.get("history") or [])
    old = state.get("voyage_id")
    if old and old != str(voyage_id) and old not in history:
        history.append(old)
    phases[phase] = {
        **state,
        "voyage_id": str(voyage_id),
        "attempts": int(state.get("attempts", 0) or 0) + (0 if old == str(voyage_id) else 1),
        "history": history,
        **extra,
    }
    return phases


async def _start_survey(
    session: AsyncSession,
    run: ResearchRun,
    brief: ResearchBrief,
    enqueue: Enqueue | None,
    *,
    broaden: bool = False,
) -> VoyageRun:
    """建文献检索任务并排上队。库上已经有一条没结束的检索任务就认领它
    （上一次建完任务、还没来得及记到这一行上就断了）。"""
    library = await session.get(DirectionLibrary, run.library_id)
    if library is None:
        raise ValueError("研究的文献库不存在")
    if not brief.include_keywords and not brief.arxiv_categories:
        raise ValueError("没有检索关键词，也没有 arXiv 分类，无法检索")
    voyage = await ingest_service.find_running_ingest_for_library(session, library.id)
    if voyage is None:
        project = await session.get(Project, run.project_id)
        knobs = IngestKnobs(
            months_back=max(brief.months_back, BROADEN_MONTHS) if broaden else brief.months_back,
            max_papers=brief.max_papers,
            relevance_threshold=(
                min(brief.relevance_threshold, BROADEN_THRESHOLD)
                if broaden
                else brief.relevance_threshold
            ),
            compile_top_n=brief.compile_top_n,
        )
        voyage = await ingest_service.create_ingest_voyage(
            session,
            library=library,
            project=project,
            mode="search",
            knobs=knobs,
            created_by=run.created_by,
            query_terms=list(brief.include_keywords) or None,
        )
    run.phases = _with_phase_voyage(run, "survey", voyage.id, broadened=broaden)
    run.phase, run.status, run.attention = "survey", "running", None
    await session.commit()
    if enqueue is not None and voyage.status in IN_FLIGHT_STATUSES:
        await enqueue("run_voyage", str(voyage.id))
    return voyage


def _discovery_direction(run: ResearchRun, brief: ResearchBrief | None) -> str:
    """交给假设探索的方向：用户原文 + 英文关键词。论文是英文的，方向是中文时带上
    英文关键词，检索和生成都更容易对上。"""
    direction = run.direction.strip()
    if brief is None or not brief.include_keywords:
        return direction
    return f"{direction}（{'; '.join(brief.include_keywords[:6])}）"


async def _start_hypotheses(
    session: AsyncSession,
    run: ResearchRun,
    enqueue: Enqueue | None,
    *,
    rounds: int | None = None,
    adopt: bool = True,
) -> VoyageRun:
    """建寻找假设的任务并排上队。库里可检索的论文不够时抛 LibraryTooSmallError。"""
    if run.library_id is None:
        raise ValueError("研究还没有文献库")
    brief = ResearchBrief.model_validate(run.brief) if run.brief else None
    voyage = None
    if adopt:
        # 上一次建完任务、还没记到这一行上就断了：认领那条，不重复建
        known = {
            str(_phase_state(run, "hypotheses").get("voyage_id") or ""),
            *(_phase_state(run, "hypotheses").get("history") or []),
        }
        rows = (
            (
                await session.execute(
                    select(VoyageRun)
                    .where(
                        VoyageRun.kind == "discovery",
                        VoyageRun.project_id == run.project_id,
                        VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
                    )
                    .order_by(VoyageRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            params = (row.checkpoint or {}).get("params") or {}
            if params.get("research_run_id") == str(run.id) and str(row.id) not in known:
                voyage = row
                break
    if voyage is None:
        await voyages_service.ensure_library_can_ground(session, run.library_id)
        data = VoyageCreate(
            kind="discovery",
            project_id=run.project_id,
            goal=run.direction.strip()[:500],
            params={
                "direction": _discovery_direction(run, brief),
                "library_id": str(run.library_id),
                "max_expansions": rounds or DISCOVERY_ROUNDS,
                "tournament": False,
                "emit_ideas": True,
                "language": brief.language if brief else None,
                "research_run_id": str(run.id),
            },
        )
        voyage = await voyages_service.create_voyage(session, created_by=run.created_by, data=data)
    run.phases = _with_phase_voyage(run, "hypotheses", voyage.id)
    run.phase, run.status, run.attention = "hypotheses", "running", None
    await session.commit()
    if enqueue is not None and voyage.status in IN_FLIGHT_STATUSES:
        await enqueue("run_voyage", str(voyage.id))
    return voyage


def _plan_error_record(exc: BaseException) -> dict[str, Any]:
    found = classify_failure(exc)
    return {
        "code": found.code,
        "message": found.user_message or str(exc)[:300],
        "transient": found.transient,
        # 密钥不对、没配模型、请求本身不被接受：再试也一样，不必等三次
        "fatal": found.code in AUTH_CODES or found.code in ("BAD_REQUEST", "LLM_BAD_REQUEST"),
        "at": utcnow().isoformat(),
    }


async def _run_plan(session: AsyncSession, run: ResearchRun, enqueue: Enqueue | None) -> bool:
    """确定范围：推出简报 → 给课题改名 → 建库并关联 → 建文献检索任务。

    每一小步做完就提交，而且都先看「是不是已经做过」，所以中途断掉后再来一次会从
    断的地方接着做。失败记一次（三次后停下来等用户），返回 False。
    """
    run_id = run.id
    try:
        project = await session.get(Project, run.project_id)
        if project is None:
            raise ValueError("研究所属的课题不存在")
        plan = _phase_state(run, "plan")
        if run.brief is None:
            brief = await derive_brief(
                session,
                run.direction,
                overrides=plan.get("overrides"),
                user_id=run.created_by,
                project_id=run.project_id,
            )
            run.brief = brief.model_dump()
            # 用户这期间自己改过名字就不动（和当初起的临时名字对比）
            if project.name == plan.get("provisional_name"):
                project.name = brief.title[:255]
            await session.commit()
        brief = ResearchBrief.model_validate(run.brief)
        if run.library_id is None:
            library = (
                await session.execute(
                    select(DirectionLibrary).where(DirectionLibrary.project_id == run.project_id)
                )
            ).scalar_one_or_none()
            if library is None:
                fields = _library_fields(brief)
                library = await libraries_service.create_library(
                    session,
                    name=project.name,
                    statement=fields["statement"],
                    cadence=fields["cadence"],
                    keywords=fields["keywords"],
                    discipline=fields["discipline"],
                    created_by=run.created_by,
                    origin_project_id=run.project_id,
                )
                definition = dict(library.definition or {})
                for key in ("questions", "in_scope", "out_of_scope"):
                    definition[key] = fields[key]
                library.definition = definition
            await libraries_service.set_source_libraries(
                session, topic_id=run.project_id, library_ids=[library.id]
            )
            run.library_id = library.id
            await session.commit()
        await _start_survey(session, run, brief, enqueue)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 记一次失败，等下一次再试
        logger.warning("research %s: planning failed", run_id, exc_info=True)
        await session.rollback()
        run = await session.get(ResearchRun, run_id, populate_existing=True)
        if run is None:
            return False
        phases = copy.deepcopy(run.phases) if isinstance(run.phases, dict) else {}
        plan = dict(phases.get("plan") or {})
        plan["attempts"] = int(plan.get("attempts", 0) or 0) + 1
        plan["last_error"] = _plan_error_record(exc)
        phases["plan"] = plan
        run.phases = phases
        await session.commit()
        return False


# ---- 推进 ----

_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _lock_for(run_id: uuid.UUID) -> asyncio.Lock:
    """同一次研究的推进串行（引擎是单进程）：任务收尾、定时任务、接口可能同时来，
    不串行的话两边都看到「该建下一阶段的任务了」，就会建出两条。"""
    lock = _locks.get(run_id)
    if lock is None:
        lock = _locks[run_id] = asyncio.Lock()
    return lock


async def _apply(session: AsyncSession, run: ResearchRun, decision: Decision) -> None:
    if (run.phase, run.status, run.attention) == (
        decision.phase,
        decision.status,
        decision.attention,
    ):
        return
    run.phase, run.status, run.attention = decision.phase, decision.status, decision.attention
    await session.commit()


async def advance(
    session: AsyncSession, run_id: uuid.UUID, enqueue: Enqueue | None = None
) -> ResearchRun | None:
    """看一遍现状，把这次研究推到它该在的地方。可以随时、重复调用。"""
    async with _lock_for(run_id):
        run = await session.get(ResearchRun, run_id, populate_existing=True)
        if run is None:
            return None
        for _ in range(4):  # plan → survey → …：一次最多连走几步，防意外打转
            decision = decide(run.phase, run.status, await gather_facts(session, run))
            if decision.action == "plan":
                progressed = await _run_plan(session, run, enqueue)
                run = await session.get(ResearchRun, run_id, populate_existing=True)
                if run is None:
                    return None
                if progressed:
                    continue
                # 这次没做成：次数用完（或不必再试）就转成「需要处理」，否则等下一次
                after = decide(run.phase, run.status, await gather_facts(session, run))
                if after.action != "plan":
                    await _apply(session, run, after)
                break
            if decision.action == "start_hypotheses":
                await _start_hypotheses(session, run, enqueue)
                continue
            await _apply(session, run, decision)
            break
        return run


async def active_run_ids(session: AsyncSession) -> list[uuid.UUID]:
    """定时任务要看的研究：没被停止的全部。已完成的也看——用户取消选定后它该回到
    「等你选定」。"""
    return list(
        (
            await session.execute(
                select(ResearchRun.id)
                .where(ResearchRun.status != "cancelled")
                .order_by(ResearchRun.created_at)
            )
        )
        .scalars()
        .all()
    )


async def find_by_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> uuid.UUID | None:
    """哪次研究的当前阶段是这条任务（任务收尾时用）。"""
    rows = (
        (await session.execute(select(ResearchRun).where(ResearchRun.status != "cancelled")))
        .scalars()
        .all()
    )
    for run in rows:
        if current_voyage_id(run) == voyage_id:
            return run.id
    return None


# ---- 接口用的操作 ----


def _normalize(direction: str) -> str:
    return " ".join(direction.split()).casefold()


async def create_run(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    direction: str,
    client_key: str | None,
    brief: ResearchBriefInput | None,
) -> tuple[ResearchRun, bool]:
    """建一次研究（课题 + 这一行）。返回 (行, 是否新建)。

    幂等：同一个 client_key 再来返回原来那一行；没带 key 时，同一个人 60 秒内提交
    同样的方向（忽略空白和大小写）、且那次没被停止，也返回原来那一行。
    """
    direction = direction.strip()
    if client_key:
        existing = (
            await session.execute(select(ResearchRun).where(ResearchRun.client_key == client_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
    else:
        cutoff = utcnow() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
        recent = (
            (
                await session.execute(
                    select(ResearchRun)
                    .where(
                        ResearchRun.created_by == user_id,
                        ResearchRun.status != "cancelled",
                        ResearchRun.created_at >= cutoff,
                    )
                    .order_by(ResearchRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for row in recent:
            if _normalize(row.direction) == _normalize(direction):
                return row, False
    provisional = direction[:PROVISIONAL_NAME_CHARS]
    project = await projects_service.create_project(
        session, user_id, ProjectCreate(name=provisional, statement=direction[:2000])
    )
    overrides = brief.model_dump(exclude_none=True) if brief is not None else {}
    run = ResearchRun(
        project_id=project.id,
        direction=direction,
        phase="plan",
        status="running",
        phases={"plan": {"attempts": 0, "provisional_name": provisional, "overrides": overrides}},
        client_key=client_key,
        created_by=user_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run, True


async def cancel_run(session: AsyncSession, run: ResearchRun) -> ResearchRun:
    """停止：这一行记成已停止，当前阶段的任务一并取消。已找到的论文和想法都留着。"""
    if run.status == "cancelled":
        raise ResearchError("RESEARCH_ALREADY_STOPPED")
    async with _lock_for(run.id):
        voyage = None
        if run.phase != "choose":
            voyage = await _load_voyage(session, current_voyage_id(run))
        run.status, run.attention = "cancelled", None
        await session.commit()
        if voyage is not None and voyage.status not in TERMINAL_STATUSES:
            await voyages_service.cancel_voyage(session, voyage)
            checkpoint = dict(voyage.checkpoint or {})
            if clear_auto_retry(checkpoint):
                voyage.checkpoint = checkpoint
                await session.commit()
        await session.refresh(run)
    return run


async def _resume_child(session: AsyncSession, voyage: VoyageRun, enqueue: Enqueue) -> None:
    """把停在出错状态的任务从断点接着跑（与任务页的「重试」同一套）。"""
    checkpoint = dict(voyage.checkpoint or {})
    if clear_auto_retry(checkpoint):
        voyage.checkpoint = checkpoint
    voyage.status = "executing"
    await session.commit()
    await enqueue("resume_voyage", str(voyage.id))


async def retry_run(
    session: AsyncSession,
    run: ResearchRun,
    enqueue: Enqueue,
    *,
    brief: ResearchBriefInput | None = None,
    phase: str | None = None,
    broaden: bool | None = None,
) -> ResearchRun:
    """重试当前阶段，或者按改过的简报重做。

    - 确定范围：次数清零再来（给了简报就用它）；
    - 检索文献：任务停在出错状态就从断点接着跑；否则新建一条检索任务（旧的留在
      history 里）。上次是「相关的太少」时自动放宽（门槛 0.5、回溯 60 个月）；
    - 寻找假设：同上，接着跑或新建一条；
    - 给了改过的简报（或 phase="survey"）：不管现在在哪个阶段，都回到检索文献。

    这个阶段的任务还在跑时不接受（409 RESEARCH_PHASE_RUNNING）：先停止它。
    """
    async with _lock_for(run.id):
        overrides = brief.model_dump(exclude_none=True) if brief is not None else {}
        redo_survey = bool(overrides or broaden) and run.phase != "plan"
        target = phase or ("survey" if redo_survey else run.phase)
        if target == "choose":
            if run.status != "cancelled":
                raise ResearchError("RESEARCH_NOTHING_TO_RETRY")
            # 停在「等你选定」时被停止的：想法都在，恢复就是回到等选定
            _ideas, picked = await _idea_counts(session, run.project_id)
            run.status = "done" if picked >= 1 else "waiting_user"
            await session.commit()
            return run
        if run.phase == "plan" or run.library_id is None or run.brief is None:
            # 范围还没定下来：从确定范围重来
            phases = copy.deepcopy(run.phases) if isinstance(run.phases, dict) else {}
            plan = dict(phases.get("plan") or {})
            plan["attempts"], plan["last_error"] = 0, None
            if overrides:
                plan["overrides"] = {**(plan.get("overrides") or {}), **overrides}
                run.brief = None
            phases["plan"] = plan
            run.phases, run.phase, run.status, run.attention = phases, "plan", "running", None
            await session.commit()
            await enqueue("research_advance", str(run.id), _job_id=f"research-{run.id}")
            return run

        current = await _load_voyage(session, current_voyage_id(run))
        if current is not None and (
            current.status in IN_FLIGHT_STATUSES or current.status in ("paused_ask", "paused_gate")
        ):
            raise ResearchError("RESEARCH_PHASE_RUNNING")

        if target == "survey":
            was_too_few = (run.attention or {}).get("code") == "SURVEY_TOO_FEW_RELEVANT"
            same_phase = run.phase == "survey"
            if (
                same_phase
                and not overrides
                and not broaden
                and current is not None
                and current.status == "paused_error"
            ):
                run.status, run.attention = "running", None
                await session.commit()
                await _resume_child(session, current, enqueue)
                return run
            if overrides:
                merged = await finalize_brief(session, run.direction, dict(run.brief), overrides)
                run.brief = merged.model_dump()
                library = await session.get(DirectionLibrary, run.library_id)
                if library is not None:
                    await libraries_service.update_library(
                        session, library=library, fields=_library_fields(merged)
                    )
            brief_now = ResearchBrief.model_validate(run.brief)
            # 库上那条旧的检索任务还停着（出错状态）：它占着这个库，先取消
            stale = await ingest_service.find_running_ingest_for_library(session, run.library_id)
            if stale is not None:
                await voyages_service.cancel_voyage(session, stale)
            do_broaden = bool(broaden) if broaden is not None else (was_too_few and not overrides)
            await _start_survey(session, run, brief_now, enqueue, broaden=do_broaden)
            return run

        # hypotheses
        if run.phase == "hypotheses" and current is not None and current.status == "paused_error":
            run.status, run.attention = "running", None
            await session.commit()
            await _resume_child(session, current, enqueue)
            return run
        await _start_hypotheses(session, run, enqueue, adopt=False)
        return run


async def more_ideas(
    session: AsyncSession, run: ResearchRun, enqueue: Enqueue, *, rounds: int | None
) -> ResearchRun:
    """在同一个文献库上再找一批假设（新建一条任务；与已有想法重复的不会重复写）。"""
    async with _lock_for(run.id):
        if run.library_id is None or run.phase not in ("hypotheses", "choose"):
            raise ResearchError("RESEARCH_NOT_READY")
        current = await _load_voyage(session, _phase_voyage_id(run, "hypotheses"))
        if current is not None and current.status not in TERMINAL_STATUSES:
            raise ResearchError("RESEARCH_PHASE_RUNNING")
        await _start_hypotheses(session, run, enqueue, rounds=rounds, adopt=False)
    return run


async def delete_run(session: AsyncSession, run: ResearchRun, *, purge: bool = False) -> None:
    """删掉这次研究的记录。默认只删这一行：课题、文献库、论文和想法都留着，在原来的
    页面里照常能用。``purge=True`` 连课题和它的文献库一起删。

    当前阶段的任务还没结束就先取消它——留一条没人管的任务在跑没有意义。
    """
    async with _lock_for(run.id):
        voyage = await _load_voyage(session, current_voyage_id(run))
        if voyage is not None and voyage.status not in TERMINAL_STATUSES:
            await voyages_service.cancel_voyage(session, voyage)
        project_id, library_id = run.project_id, run.library_id
        await session.delete(run)
        await session.commit()
        if purge:
            if library_id is not None:
                library = await session.get(DirectionLibrary, library_id)
                if library is not None:
                    await libraries_service.delete_library(session, library=library, force=True)
                    await session.commit()
            project = await session.get(Project, project_id)
            if project is not None:
                await projects_service.delete_project(session, project)
    _locks.pop(run.id, None)


async def rename_run(session: AsyncSession, run: ResearchRun, name: str) -> ResearchRun:
    """改名：课题和它的文献库一起改。"""
    name = name.strip()
    project = await session.get(Project, run.project_id)
    if project is not None:
        project.name = name
    if run.library_id is not None:
        library = await session.get(DirectionLibrary, run.library_id)
        if library is not None:
            library.name = name
    await session.commit()
    return run


# ---- 读：列表与详情 ----


async def get_owned_run(
    session: AsyncSession, run_id: uuid.UUID, user_id: uuid.UUID
) -> ResearchRun | None:
    return (
        await session.execute(
            select(ResearchRun).where(
                ResearchRun.id == run_id,
                projects_service.in_my_projects(ResearchRun.project_id, user_id),
            )
        )
    ).scalar_one_or_none()


async def list_runs(session: AsyncSession, user_id: uuid.UUID) -> list[ResearchRun]:
    return list(
        (
            await session.execute(
                select(ResearchRun)
                .where(projects_service.in_my_projects(ResearchRun.project_id, user_id))
                .order_by(ResearchRun.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )


def _derived_status(run: ResearchRun, picked: int) -> str:
    """给界面的状态。只有一处和存的不一样：等你选定 ↔ 已完成 跟着「有没有选定的
    想法」当场变——用户点了选定，下一次读就该是已完成，不等每分钟的对账。"""
    if run.phase == "choose" and run.status in ("waiting_user", "done"):
        return "done" if picked >= 1 else "waiting_user"
    return run.status


async def to_read(session: AsyncSession, run: ResearchRun) -> ResearchRunRead:
    project = await session.get(Project, run.project_id)
    papers = 0
    if run.library_id is not None:
        papers = (await ingest_service.library_paper_counts(session, run.library_id))["library"]
    ideas, picked = await _idea_counts(session, run.project_id)
    return ResearchRunRead(
        id=run.id,
        project_id=run.project_id,
        library_id=run.library_id,
        name=project.name if project is not None else run.direction[:PROVISIONAL_NAME_CHARS],
        direction=run.direction,
        phase=run.phase,
        status=_derived_status(run, picked),
        attention=ResearchAttention.model_validate(run.attention) if run.attention else None,
        counts=ResearchCounts(papers=papers, ideas=ideas, picked=picked),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


# 估费用的标价（美元 / 百万 token）：claude-opus-5 输入 5、输出 25
_USD_PER_MTOK_IN = 5.0
_USD_PER_MTOK_OUT = 25.0


def _usage(*voyages: VoyageRun | None) -> UsageRead:
    prompt = completion = 0
    for voyage in voyages:
        raw = voyage.usage if voyage is not None and isinstance(voyage.usage, dict) else {}
        prompt += int(raw.get("prompt_tokens", 0) or 0)
        completion += int(raw.get("completion_tokens", 0) or 0)
    return UsageRead(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=round(
            prompt * _USD_PER_MTOK_IN / 1e6 + completion * _USD_PER_MTOK_OUT / 1e6, 2
        ),
    )


def _times(voyage: VoyageRun | None) -> dict[str, Any]:
    if voyage is None:
        return {}
    finished = voyage.updated_at if voyage.status in TERMINAL_STATUSES else None
    return {"started_at": voyage.created_at, "finished_at": finished}


async def _all_phase_voyages(session: AsyncSession, run: ResearchRun) -> list[VoyageRun]:
    ids: list[uuid.UUID] = []
    for phase in ("survey", "hypotheses"):
        state = _phase_state(run, phase)
        for raw in [state.get("voyage_id"), *(state.get("history") or [])]:
            try:
                ids.append(uuid.UUID(str(raw)))
            except (TypeError, ValueError):
                continue
    if not ids:
        return []
    return list(
        (await session.execute(select(VoyageRun).where(VoyageRun.id.in_(ids)))).scalars().all()
    )


async def _current_step(session: AsyncSession, voyage: VoyageRun | None) -> dict[str, Any]:
    """正在做（或停在）哪一步：标题给日志看，动作名给界面按它出自己的说法。"""
    if voyage is None or voyage.status in TERMINAL_STATUSES:
        return {}
    step = await _first_open_step(session, voyage.id)
    if step is None:
        return {}
    return {"step_title": step.title, "step_action": step.action}


async def _survey_read(
    session: AsyncSession, run: ResearchRun, brief: ResearchBrief | None
) -> SurveyRead:
    voyage = await _load_voyage(session, _phase_voyage_id(run, "survey"))
    out = SurveyRead(
        voyage_id=voyage.id if voyage is not None else None,
        state=voyage.status if voyage is not None else None,
        usage=_usage(voyage),
        **await _current_step(session, voyage),
        **_times(voyage),
    )
    if run.library_id is None:
        return out
    counts = await ingest_service.library_paper_counts(session, run.library_id)
    kept = counts["library"]
    top_n = brief.compile_top_n if brief is not None else kept
    out.progress = SurveyProgress(
        found=counts["total"],
        screened=counts["total"] - counts["candidate"],
        kept=kept,
        summarised=counts["compiled"],
        to_summarise=min(top_n, kept),
    )
    rows = (
        await session.execute(
            select(Paper, LibraryPaper.relevance_score)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(
                LibraryPaper.library_id == run.library_id,
                LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
            )
            .order_by(LibraryPaper.relevance_score.desc().nulls_last(), Paper.id)
            .limit(8)
        )
    ).all()
    out.top_papers = [
        TopPaper(id=p.id, title=p.title, year=p.year, tldr=p.tldr, relevance=score)
        for p, score in rows
    ]
    stats = (voyage.checkpoint or {}).get("ingest_search_stats") if voyage is not None else None
    if isinstance(stats, dict):
        out.notes = [str(m) for m in (stats.get("messages") or [])]
        errors = {
            str(e.get("source")): str(e.get("error") or "")
            for e in (stats.get("source_errors") or [])
            if isinstance(e, dict)
        }
        fetched = stats.get("source_counts") if isinstance(stats.get("source_counts"), dict) else {}
        names = list(dict.fromkeys([*fetched, *errors]))
        out.sources = [
            SourceResult(source=s, fetched=int(fetched.get(s, 0) or 0), error=errors.get(s))
            for s in names
        ]
    return out


async def _hypotheses_read(session: AsyncSession, run: ResearchRun) -> HypothesesRead:
    voyage = await _load_voyage(session, _phase_voyage_id(run, "hypotheses"))
    out = HypothesesRead(
        voyage_id=voyage.id if voyage is not None else None,
        state=voyage.status if voyage is not None else None,
        usage=_usage(voyage),
        **await _current_step(session, voyage),
        **_times(voyage),
    )
    if voyage is None:
        return out
    checkpoint = voyage.checkpoint if isinstance(voyage.checkpoint, dict) else {}
    rounds = (checkpoint.get("discovery") or {}).get("rounds")
    out.round = len(rounds) if isinstance(rounds, dict) else 0
    try:
        out.max_rounds = int((checkpoint.get("params") or {}).get("max_expansions") or 0)
    except (TypeError, ValueError):
        out.max_rounds = 0
    out.node_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(HypothesisNode)
                .where(HypothesisNode.run_id == voyage.id, HypothesisNode.kind == "hypothesis")
            )
        ).scalar_one()
    )
    return out


def _evidence_counts(idea: Idea) -> EvidenceCounts:
    support: set[str] = set()
    refute: set[str] = set()
    for entry in idea.evidence or []:
        if not isinstance(entry, dict) or not entry.get("paper_id"):
            continue
        if entry.get("stance") == "refute":
            refute.add(str(entry["paper_id"]))
        else:  # 想法生成写的依据没有立场字段：都算支持
            support.add(str(entry["paper_id"]))
    origin = idea.origin if isinstance(idea.origin, dict) else {}
    unsupported = origin.get("unsupported_subclaims")
    return EvidenceCounts(
        support=len(support),
        refute=len(refute),
        unsupported=len(unsupported) if isinstance(unsupported, list) else 0,
    )


async def to_detail(
    session: AsyncSession, run: ResearchRun, *, user_id: uuid.UUID
) -> ResearchRunDetail:
    base = await to_read(session, run)
    brief = ResearchBrief.model_validate(run.brief) if run.brief else None
    ideas = (
        (
            await session.execute(
                select(Idea)
                .where(Idea.project_id == run.project_id, Idea.trashed_at.is_(None))
                .order_by(Idea.created_at, Idea.id)
            )
        )
        .scalars()
        .all()
    )
    current = await _load_voyage(session, current_voyage_id(run))
    open_ask = None
    if current is not None and current.status == "paused_ask":
        ask = await messages_service.open_ask(session, current.id)
        open_ask = VoyageMessageRead.model_validate(ask) if ask is not None else None
    next_retry_at = None
    retry_attempt = 0
    last_error = None
    if current is not None and current.status == "paused_error":
        next_retry_at = auto_retry_due_at(current.checkpoint)
        retry_attempt = auto_retry_attempt(current.checkpoint)
        raw = (current.checkpoint or {}).get("last_error")
        if isinstance(raw, dict) and raw.get("code"):
            last_error = LastErrorRead(
                code=str(raw["code"]),
                message=str(raw.get("message") or ""),
                transient=bool(raw.get("transient")),
                source=str(raw["source"]) if raw.get("source") else None,
            )
    elif run.phase == "plan" and isinstance(_phase_state(run, "plan").get("last_error"), dict):
        raw = _phase_state(run, "plan")["last_error"]
        last_error = LastErrorRead(
            code=str(raw.get("code") or "PLAN_FAILED"),
            message=str(raw.get("message") or ""),
            transient=bool(raw.get("transient")),
            source="planner",
        )
    has_machine = (
        await session.execute(
            select(ConnectionCredential.id).where(ConnectionCredential.user_id == user_id).limit(1)
        )
    ).first() is not None
    return ResearchRunDetail(
        **base.model_dump(),
        brief=brief,
        survey=await _survey_read(session, run, brief),
        hypotheses=await _hypotheses_read(session, run),
        ideas=[
            ResearchIdea(
                id=i.id,
                title=i.title,
                summary=i.summary or "",
                scores=i.scores,
                status=i.status,
                evidence_counts=_evidence_counts(i),
                origin=i.origin if isinstance(i.origin, dict) else None,
                source_voyage_id=i.source_voyage_id,
                created_at=i.created_at,
            )
            for i in ideas
        ],
        open_ask=open_ask,
        next_retry_at=next_retry_at,
        retry_attempt=retry_attempt,
        last_error=last_error,
        usage=_usage(*await _all_phase_voyages(session, run)),
        capabilities=Capabilities(
            remote_machine=has_machine, latex=shutil.which("tectonic") is not None
        ),
    )
