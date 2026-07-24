"""方向文献库解析与成员行工具（不 import fastapi）。

P7 起课题 × 库多对多关联（``topic_source_libraries``）：课题的语料 = 关联库论文
的并集，经 ``get_source_libraries``/``get_source_library_ids`` 取数（空关联=
无语料，调用方应给空态而非报错）。``get_library_for_project`` 是历史单库解析
（起源库优先、否则第一个关联库、否则 None），逐步只供管理/ingest 路径使用——
读路径（想法生成/检索/图谱/写作引用等）应改走关联库并集。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.library_direction import (
    DirectionLibrary,
    DirectionLibraryCurator,
    LibraryPaper,
    TopicSourceLibrary,
)
from app.models.paper import Concept, Paper
from app.models.project import Project, ProjectMember
from app.models.user import User

logger = logging.getLogger(__name__)


def library_definition(library: DirectionLibrary) -> dict[str, Any]:
    """库的收录配置（P8a 权威源）：definition JSON；为空时回退标量列拼一份兼容视图。

    ingest（检索/扩展/打分/编译）与 build_relevance_context 一律经此取 statement/
    rubric/anchor_papers/keywords/questions/cadence，不再读起源课题 project.definition。
    """
    definition = library.definition if isinstance(library.definition, dict) else {}
    if definition:
        return definition
    # 回退：老库或迁移前建的库 definition 为空 → 用标量列拼最小可用配置。
    fallback: dict[str, Any] = {}
    if library.statement:
        fallback["statement"] = library.statement
    if library.rubric:
        fallback["rubric"] = library.rubric
    if library.anchors:
        fallback["anchor_papers"] = library.anchors
    if library.cadence:
        fallback["cadence"] = library.cadence
    return fallback


async def get_library_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> DirectionLibrary | None:
    """解析课题的「管理库」：起源库优先（project_id 直接回指），否则取第一个
    关联库（按关联建立时间），都没有则 None。

    P7 起管理/ingest 路径专用（历史 1:1 语义单库解析）；并集读路径改用
    ``get_source_libraries``/``get_source_library_ids``。不再兜底自动建库——
    P9c 起课题创建不再自动建隐式库/建关联，缺失即代表课题真的没有语料（存量
    隐式库仍靠 project_id 回指解析，是带起源溯源的普通独立库）。
    """
    stmt = select(DirectionLibrary).where(DirectionLibrary.project_id == project_id)
    library = (await session.execute(stmt)).scalar_one_or_none()
    if library is not None:
        return library
    libraries = await get_source_libraries(session, project_id)
    return libraries[0] if libraries else None


async def get_library_id_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> uuid.UUID | None:
    library = await get_library_for_project(session, project_id)
    return library.id if library else None


async def get_source_library_ids(session: AsyncSession, topic_id: uuid.UUID) -> list[uuid.UUID]:
    """课题关联的全部库 id（按关联建立时间；空=无语料）。"""
    stmt = (
        select(TopicSourceLibrary.library_id)
        .where(TopicSourceLibrary.topic_id == topic_id)
        .order_by(TopicSourceLibrary.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_source_libraries(
    session: AsyncSession, topic_id: uuid.UUID
) -> list[DirectionLibrary]:
    """课题关联的全部库对象（按关联建立时间；空=无语料）。"""
    stmt = (
        select(DirectionLibrary)
        .join(TopicSourceLibrary, TopicSourceLibrary.library_id == DirectionLibrary.id)
        .where(TopicSourceLibrary.topic_id == topic_id)
        .order_by(TopicSourceLibrary.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_source_libraries(
    session: AsyncSession, *, topic_id: uuid.UUID, library_ids: list[uuid.UUID]
) -> None:
    """全量替换课题的关联库（去重，不存在的 library_id 静默忽略）；flush 不 commit。"""
    unique_ids = list(dict.fromkeys(library_ids))
    await session.execute(
        delete(TopicSourceLibrary).where(TopicSourceLibrary.topic_id == topic_id)
    )
    if unique_ids:
        found = set(
            (
                await session.execute(
                    select(DirectionLibrary.id).where(DirectionLibrary.id.in_(unique_ids))
                )
            )
            .scalars()
            .all()
        )
        for library_id in unique_ids:
            if library_id in found:
                session.add(TopicSourceLibrary(topic_id=topic_id, library_id=library_id))
    await session.flush()


async def get_membership(
    session: AsyncSession, *, library_id: uuid.UUID, paper_id: uuid.UUID
) -> LibraryPaper | None:
    stmt = select(LibraryPaper).where(
        LibraryPaper.library_id == library_id, LibraryPaper.paper_id == paper_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def ensure_membership(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    status: str = "candidate",
    **fields: Any,
) -> tuple[LibraryPaper, bool]:
    """成员行 get-or-create（flush 不 commit），返回 (行, 是否新建)。"""
    membership = await get_membership(session, library_id=library_id, paper_id=paper_id)
    if membership is not None:
        return membership, False
    membership = LibraryPaper(library_id=library_id, paper_id=paper_id, status=status, **fields)
    session.add(membership)
    await session.flush()
    return membership, True


async def membership_for_project(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> LibraryPaper | None:
    """课题关联库并集里该论文的成员行（工具层「论文是否在本课题语料内」的统一检查）。

    跨库同一论文取确定性视角（有 wiki 优先、其次相关性高，见 ``membership_rank``）；
    课题没有任何关联库或论文不在其中 → None（视为不在语料内，不报错）。
    """
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return None
    rows = (
        (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id.in_(library_ids),
                    LibraryPaper.paper_id == paper_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return min(rows, key=membership_rank)


async def find_pool_paper(
    session: AsyncSession,
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    dedup_key: str | None = None,
) -> Paper | None:
    """按 arxiv → doi → dedup_key 优先级查全局内容池（写路径「先查池」的统一入口）。"""
    if arxiv_id:
        stmt = select(Paper).where(Paper.arxiv_id == arxiv_id).limit(1)
        if (paper := (await session.execute(stmt)).scalars().first()) is not None:
            return paper
    if doi:
        stmt = select(Paper).where(func.lower(Paper.doi) == doi.lower()).limit(1)
        if (paper := (await session.execute(stmt)).scalars().first()) is not None:
            return paper
    if dedup_key:
        stmt = select(Paper).where(Paper.dedup_key == dedup_key).limit(1)
        return (await session.execute(stmt)).scalars().first()
    return None


def member_paper_stmt(library_id: uuid.UUID) -> Select:
    """库内论文基础查询：SELECT (Paper, LibraryPaper) 按成员表过滤。"""
    return (
        select(Paper, LibraryPaper)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(LibraryPaper.library_id == library_id)
    )


def member_papers_stmt(library_ids: Sequence[uuid.UUID]) -> Select:
    """关联库并集内论文基础查询：SELECT (Paper, LibraryPaper)，跨库同一论文各一行
    （调用方按 :func:`dedupe_member_rows` 归并出确定性单行视角）。"""
    return (
        select(Paper, LibraryPaper)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(LibraryPaper.library_id.in_(library_ids))
    )


def membership_rank(membership: LibraryPaper) -> tuple[int, float, str]:
    """跨库同一论文的确定性视角优先级（越小越优）：有 wiki 优先、其次相关性分高、
    再次 library_id 稳定序（docs-dev/workspace-ia-redesign.md §3.4 展示优先级）。"""
    return (
        0 if membership.wiki_content else 1,
        -(membership.relevance_score if membership.relevance_score is not None else -1e18),
        str(membership.library_id),
    )


def dedupe_member_rows(
    rows: Iterable[tuple[Paper, LibraryPaper]],
) -> list[tuple[Paper, LibraryPaper]]:
    """并集读取的 (Paper, LibraryPaper) 行按 paper 归并成单行（membership_rank 取最优）。

    入库顺序不定，返回顺序按首次出现稳定（不排序，调用方自行排序）。"""
    best: dict[uuid.UUID, tuple[Paper, LibraryPaper]] = {}
    for paper, membership in rows:
        current = best.get(paper.id)
        if current is None or membership_rank(membership) < membership_rank(current[1]):
            best[paper.id] = (paper, membership)
    return list(best.values())


def user_visible_paper_stmt(user_id: uuid.UUID) -> Select:
    """用户可管理论文（其所属方向的库 ∪ 被任命管理的库 ∪ 平台 admin 全库）
    的成员行：SELECT (Paper, LibraryPaper, project_id)。P6 起策展人/管理员与
    成员同权（docs-dev/workspace-ia-redesign.md §5）。"""
    my_projects = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    my_curated = select(DirectionLibraryCurator.library_id).where(
        DirectionLibraryCurator.user_id == user_id
    )
    is_admin = select(User.id).where(User.id == user_id, User.role == "admin").exists()
    return (
        select(Paper, LibraryPaper, DirectionLibrary.project_id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .where(
            or_(
                DirectionLibrary.project_id.in_(my_projects),
                DirectionLibrary.id.in_(my_curated),
                is_admin,
            )
        )
    )


# ---- 共享方向库读视图（P5c：全实验室可读，docs-dev/workspace-ia-redesign.md §2/§5） ----


def _last_synced_of(ingest_state: Any) -> Any:
    """从 ingest_state 提取「上次同步时间」：优先 last_run.finished_at，退回 watermark。"""
    if not isinstance(ingest_state, dict):
        return None
    last_run = ingest_state.get("last_run")
    if isinstance(last_run, dict) and last_run.get("finished_at"):
        return last_run["finished_at"]
    return ingest_state.get("watermark")


async def get_library(session: AsyncSession, library_id: uuid.UUID) -> DirectionLibrary | None:
    return await session.get(DirectionLibrary, library_id)


async def _library_stats(
    session: AsyncSession, library_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, Any], dict[uuid.UUID, int]]:
    """批量聚合库统计：(库内论文数, 最近编译时间, 概念数)。

    论文数口径 = 相关性达标及之后（与论文列表的 library 组别名一致）。
    """
    from app.services.papers import PAPER_STATUS_GROUPS  # 延迟导入避免循环依赖

    if not library_ids:
        return {}, {}, {}
    paper_rows = await session.execute(
        select(LibraryPaper.library_id, func.count(), func.max(LibraryPaper.compiled_at))
        .where(
            LibraryPaper.library_id.in_(library_ids),
            LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
        )
        .group_by(LibraryPaper.library_id)
    )
    paper_counts: dict[uuid.UUID, int] = {}
    last_compiled: dict[uuid.UUID, Any] = {}
    for lib_id, count, compiled_at in paper_rows.all():
        paper_counts[lib_id] = int(count)
        last_compiled[lib_id] = compiled_at
    concept_rows = await session.execute(
        select(Concept.library_id, func.count())
        .where(Concept.library_id.in_(library_ids))
        .group_by(Concept.library_id)
    )
    concept_counts = {lib_id: int(count) for lib_id, count in concept_rows.all()}
    return paper_counts, last_compiled, concept_counts


async def _my_project_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    )
    return set(rows.scalars().all())


async def _my_linked_library_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """被我参与的课题关联的库 id（P7：is_mine 按关联判定，而非起源课题）。"""
    rows = await session.execute(
        select(TopicSourceLibrary.library_id)
        .join(ProjectMember, ProjectMember.project_id == TopicSourceLibrary.topic_id)
        .where(ProjectMember.user_id == user_id)
    )
    return set(rows.scalars().all())


def _overview_dict(
    library: DirectionLibrary,
    *,
    my_linked: set[uuid.UUID],
    can_manage: bool,
    paper_count: int,
    concept_count: int,
    last_compiled_at: Any,
) -> dict[str, Any]:
    return {
        "id": library.id,
        "name": library.name,
        "statement": library.statement,
        "cadence": library.cadence,
        "monthly_budget": library.monthly_budget,
        "definition": library_definition(library),
        "project_id": library.project_id,
        "status": library.status,
        "review_note": library.review_note,
        "submitted_by": library.submitted_by,
        "is_mine": library.id in my_linked,
        "can_manage": can_manage,
        "paper_count": paper_count,
        "concept_count": concept_count,
        "last_compiled_at": last_compiled_at,
        "last_synced_at": _last_synced_of(library.ingest_state),
        "created_at": library.created_at,
        "updated_at": library.updated_at,
    }


async def list_libraries_overview(session: AsyncSession, *, user: User) -> list[dict[str, Any]]:
    """全部方向库 + 概要统计（读操作对所有登录用户开放，不做成员校验）。"""
    libraries = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    paper_counts, last_compiled, concept_counts = await _library_stats(
        session, [lib.id for lib in libraries]
    )
    my_projects = await _my_project_ids(session, user.id)
    my_linked = await _my_linked_library_ids(session, user.id)
    my_curated = await _my_curated_library_ids(session, user.id)
    return [
        _overview_dict(
            lib,
            my_linked=my_linked,
            can_manage=(
                user.role == "admin"
                or lib.id in my_curated
                or lib.submitted_by == user.id
                or (lib.project_id is not None and lib.project_id in my_projects)
            ),
            paper_count=paper_counts.get(lib.id, 0),
            concept_count=concept_counts.get(lib.id, 0),
            last_compiled_at=last_compiled.get(lib.id),
        )
        for lib in libraries
        if library_visible_to(lib, user)
    ]


def library_visible_to(library: DirectionLibrary, user: User) -> bool:
    """库对请求者是否可见（P9b）：active 全员可读；pending/rejected 仅创建者 + admin 可见。"""
    if library.status == "active":
        return True
    if user.role == "admin":
        return True
    return library.submitted_by == user.id


async def library_overview(
    session: AsyncSession, *, library: DirectionLibrary, user: User
) -> dict[str, Any]:
    """单库详情概要（同列表口径）。"""
    paper_counts, last_compiled, concept_counts = await _library_stats(session, [library.id])
    my_linked = await _my_linked_library_ids(session, user.id)
    return _overview_dict(
        library,
        my_linked=my_linked,
        can_manage=await can_manage_library(session, user=user, library=library),
        paper_count=paper_counts.get(library.id, 0),
        concept_count=concept_counts.get(library.id, 0),
        last_compiled_at=last_compiled.get(library.id),
    )


async def source_libraries_overview(
    session: AsyncSession, *, topic_id: uuid.UUID, user: User
) -> list[dict[str, Any]]:
    """课题关联库 + 概要统计（同列表口径，按关联建立时间）。"""
    libraries = await get_source_libraries(session, topic_id)
    ids = [lib.id for lib in libraries]
    paper_counts, last_compiled, concept_counts = await _library_stats(session, ids)
    my_projects = await _my_project_ids(session, user.id)
    my_linked = await _my_linked_library_ids(session, user.id)
    my_curated = await _my_curated_library_ids(session, user.id)
    return [
        _overview_dict(
            lib,
            my_linked=my_linked,
            can_manage=(
                user.role == "admin"
                or lib.id in my_curated
                or (lib.project_id is not None and lib.project_id in my_projects)
            ),
            paper_count=paper_counts.get(lib.id, 0),
            concept_count=concept_counts.get(lib.id, 0),
            last_compiled_at=last_compiled.get(lib.id),
        )
        for lib in libraries
    ]


# ---- AI 一键生成收录设置（建库/编辑弹窗用；同步 LLM→JSON，失败给空兜底不抛） ----

_SUGGEST_MAX_TOKENS = 2048
_SUGGEST_MAX_CATEGORIES = 12
_SUGGEST_MAX_KEYWORDS = 30
_SUGGEST_MAX_RUBRIC = 6
_SUGGEST_MAX_ANCHORS = 8

# 标记（POLARIS_LIBRARY_SUGGEST）供 fake provider / 日志识别本次调用用途。
SUGGEST_DEFINITION_SYSTEM_PROMPT = """\
你是科研文献库收录设置助手（POLARIS_LIBRARY_SUGGEST）。用户给你一个研究方向的\
名称和一句话描述，请据此产出一套「文献库收录设置」，帮助自动检索与筛选该方向的论文。
请只输出一个 JSON 对象（不要输出任何解释性文字、不要用代码块围栏），结构如下：
{
  "keywords": {
    "arxiv_categories": ["cs.CL", "cs.AI", "cs.LG"],
    "include": ["retrieval-augmented generation", "in-context learning"]
  },
  "rubric": [
    {"name": "任务相关性", "description": "论文直接研究该方向核心任务时得高分", "weight": 0.4}
  ],
  "anchors": [
    {"title": "代表作标题", "arxiv_id": "2005.11401", "reason": "该方向奠基/代表性工作"}
  ]
}
要求：
- arxiv_categories：从名称/描述推断的相关 arXiv 分类代码（如 cs.CL、cs.AI、cs.LG、stat.ML 等），\
最相关的在前，最多 12 个；
- include：英文为主的检索关键词/术语，覆盖该方向核心概念、方法名、任务名，最多 30 个；
- rubric：3-5 条相关性打分维度，每条含 name（简短中文维度名）、description（什么样的论文在\
这一维得高分）、weight（0-1 之间的小数，各条 weight 之和约等于 1）；
- anchors：3-6 篇该方向的代表性锚点论文，每条含 title（英文原题）、arxiv_id（可留空或省略，\
不确定就不要编造）、reason（为何是该方向的锚点）；
- 无法判断某字段时给空列表，不要编造无关内容。
"""


def _empty_suggestion() -> dict[str, Any]:
    """结构完整的空兜底（解析失败/调用失败时返回，字段与成功时同形）。"""
    return {"keywords": {"arxiv_categories": [], "include": []}, "rubric": [], "anchors": []}


def _coerce_str_list(value: Any, *, cap: int) -> list[str]:
    """强制成去重保序的非空字符串列表（大小写不敏感去重），截断到 cap。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _coerce_rubric(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = item.get("description")
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError):
            weight = 0.0
        weight = min(1.0, max(0.0, weight))
        out.append(
            {
                "name": name.strip(),
                "description": description.strip() if isinstance(description, str) else "",
                "weight": weight,
            }
        )
        if len(out) >= _SUGGEST_MAX_RUBRIC:
            break
    return out


def _coerce_anchors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        arxiv_id = item.get("arxiv_id")
        reason = item.get("reason")
        out.append(
            {
                "title": title.strip(),
                "arxiv_id": (
                    arxiv_id.strip()
                    if isinstance(arxiv_id, str) and arxiv_id.strip()
                    else None
                ),
                "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
            }
        )
        if len(out) >= _SUGGEST_MAX_ANCHORS:
            break
    return out


def _parse_suggestion(content: str) -> dict[str, Any]:
    """从 LLM 输出解析收录设置 JSON；任意异常/缺字段都退回结构完整的空兜底。"""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return _empty_suggestion()
    try:
        data = json.loads(content[start : end + 1])
    except ValueError:
        return _empty_suggestion()
    if not isinstance(data, dict):
        return _empty_suggestion()
    keywords = data.get("keywords")
    keywords = keywords if isinstance(keywords, dict) else {}
    return {
        "keywords": {
            "arxiv_categories": _coerce_str_list(
                keywords.get("arxiv_categories"), cap=_SUGGEST_MAX_CATEGORIES
            ),
            "include": _coerce_str_list(keywords.get("include"), cap=_SUGGEST_MAX_KEYWORDS),
        },
        "rubric": _coerce_rubric(data.get("rubric")),
        "anchors": _coerce_anchors(data.get("anchors")),
    }


async def suggest_definition(
    *,
    name: str,
    statement: str,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """AI 根据研究方向名称 + 一句话描述生成一套收录设置（arxiv 分类/关键词/打分维度/锚点论文）。

    同步 LLM→JSON（照 affiliations 那套）：调用失败或解析不出合法结构都返回结构完整的空
    兜底而不抛，前端可直接把结果填进收录设置表单。
    """
    statement_line = (statement or "").strip() or "（未提供）"
    user_prompt = f"研究方向名称：{name.strip()}\n\n一句话描述：{statement_line}"
    try:
        result = await llm.complete(
            "librarian",
            [
                Message(role="system", content=SUGGEST_DEFINITION_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            max_tokens=_SUGGEST_MAX_TOKENS,
            user_id=user_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 生成尽力而为，失败给空兜底由前端手填
        logger.warning("LLM suggest_definition failed for %r", name, exc_info=True)
        return _empty_suggestion()
    return _parse_suggestion(result.content)


async def create_library(
    session: AsyncSession,
    *,
    name: str,
    statement: str | None = None,
    rubric: Any | None = None,
    anchors: list[Any] | None = None,
    cadence: str | None = None,
    keywords: dict[str, Any] | None = None,
    monthly_budget: int | None = None,
    created_by: uuid.UUID,
    status: str = "pending",
) -> DirectionLibrary:
    """用户独立新建方向文献库（P9b；``project_id`` 恒为 NULL——不属于任何课题，靠关联被消费）。

    P9b：任意登录用户可建，新库默认 ``status='pending'`` 待审批（仅配置，不触发抓取、
    不花 token）；创建者记为 ``submitted_by`` 并自动加为该库策展人（文献库管理员），
    以便在审批前管理自己的 pending 库。管理员审批后转 active 才能触发抓取。

    flush + refresh，不 commit（调用方 api 层负责事务收尾）。
    """
    definition: dict[str, Any] = {}
    if statement:
        definition["statement"] = statement
    if rubric:
        definition["rubric"] = rubric
    if anchors:
        definition["anchor_papers"] = anchors
    if cadence:
        definition["cadence"] = cadence
    if keywords:
        definition["keywords"] = keywords
    library = DirectionLibrary(
        name=name,
        statement=statement,
        rubric=rubric,
        anchors=anchors,
        cadence=cadence,
        definition=definition or None,  # P8a：独立库同样以 definition 为收录配置权威源
        monthly_budget=monthly_budget,
        created_by=created_by,
        submitted_by=created_by,
        status=status,
        project_id=None,
    )
    session.add(library)
    await session.flush()
    # 创建者自动成为该库策展人（幂等：避免重复主键）。
    if not await is_library_curator(session, library_id=library.id, user_id=created_by):
        session.add(DirectionLibraryCurator(library_id=library.id, user_id=created_by))
        await session.flush()
    await session.refresh(library)
    return library


async def approve_library(
    session: AsyncSession, *, library: DirectionLibrary
) -> DirectionLibrary:
    """审批通过（平台 admin）：pending/rejected → active，清空驳回理由。commit 落库。"""
    library.status = "active"
    library.review_note = None
    await session.commit()
    await session.refresh(library)
    return library


async def reject_library(
    session: AsyncSession, *, library: DirectionLibrary, note: str | None = None
) -> DirectionLibrary:
    """驳回（平台 admin）：→ rejected，记录驳回理由。commit 落库。"""
    library.status = "rejected"
    library.review_note = note
    await session.commit()
    await session.refresh(library)
    return library


# ---- P6 治理：策展人（界面叫「文献库管理员」）与库级写权限 ----


async def _my_curated_library_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(DirectionLibraryCurator.library_id).where(
            DirectionLibraryCurator.user_id == user_id
        )
    )
    return set(rows.scalars().all())


async def is_library_curator(
    session: AsyncSession, *, library_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(DirectionLibraryCurator.user_id).where(
            DirectionLibraryCurator.library_id == library_id,
            DirectionLibraryCurator.user_id == user_id,
        )
    )
    return row.first() is not None


async def _is_project_member(
    session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    return row.first() is not None


async def can_manage_library(
    session: AsyncSession, *, user: User, library: DirectionLibrary
) -> bool:
    """库级写权限（docs-dev/workspace-ia-redesign.md §5）：
    背后课题成员 ∪ 策展人（direction_library_curators）∪ 平台 admin。"""
    if user.role == "admin":
        return True
    if library.submitted_by is not None and library.submitted_by == user.id:
        return True
    if library.project_id is not None and await _is_project_member(
        session, project_id=library.project_id, user_id=user.id
    ):
        return True
    return await is_library_curator(session, library_id=library.id, user_id=user.id)


async def get_managed_project(
    session: AsyncSession, *, project_id: uuid.UUID, user: User
) -> Project | None:
    """库管理入口的统一鉴权（project 作用域的文献管理端点用）：课题成员照常放行；
    平台 admin 与该课题隐式库的策展人同权；无权限视为不存在（返回 None）。"""
    project = await session.get(Project, project_id)
    if project is None:
        return None
    if user.role == "admin":
        return project
    if await _is_project_member(session, project_id=project_id, user_id=user.id):
        return project
    library = (
        await session.execute(
            select(DirectionLibrary).where(DirectionLibrary.project_id == project_id)
        )
    ).scalar_one_or_none()
    if library is not None and await is_library_curator(
        session, library_id=library.id, user_id=user.id
    ):
        return project
    return None


async def list_curators(session: AsyncSession, library_id: uuid.UUID) -> list[dict[str, Any]]:
    stmt = (
        select(DirectionLibraryCurator.user_id, User.email, User.display_name)
        .join(User, User.id == DirectionLibraryCurator.user_id)
        .where(DirectionLibraryCurator.library_id == library_id)
        .order_by(DirectionLibraryCurator.created_at)
    )
    return [
        {"user_id": user_id, "email": email, "display_name": display_name}
        for user_id, email, display_name in (await session.execute(stmt)).all()
    ]


async def set_curators(
    session: AsyncSession, *, library: DirectionLibrary, user_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """全量替换策展人名单（平台 admin 专用）；未知 user_id 抛 ValueError。commit 落库。"""
    unique_ids = list(dict.fromkeys(user_ids))
    if unique_ids:
        found = set(
            (await session.execute(select(User.id).where(User.id.in_(unique_ids)))).scalars().all()
        )
        missing = [str(uid) for uid in unique_ids if uid not in found]
        if missing:
            raise ValueError(f"unknown user ids: {', '.join(missing)}")
    await session.execute(
        delete(DirectionLibraryCurator).where(DirectionLibraryCurator.library_id == library.id)
    )
    for uid in unique_ids:
        session.add(DirectionLibraryCurator(library_id=library.id, user_id=uid))
    await session.commit()
    return await list_curators(session, library.id)


# PATCH 顶层便捷字段 → library.definition 的键（收录配置权威源）。statement/cadence/
# rubric 同名，anchors→anchor_papers（与原 project.definition 结构一致，ingest 直接读）。
_CONFIG_TO_DEFINITION = {
    "statement": "statement",
    "cadence": "cadence",
    "rubric": "rubric",
    "anchors": "anchor_papers",
    "keywords": "keywords",
    "goals": "goals",
    "in_scope": "in_scope",
    "out_of_scope": "out_of_scope",
    "questions": "questions",
}
# definition 键 → 展示镜像标量列（overview/detail 读列，编辑时同步，避免同库内漂移）。
_DEFINITION_TO_COLUMN = {
    "statement": "statement",
    "cadence": "cadence",
    "rubric": "rubric",
    "anchor_papers": "anchors",
}


async def update_library(
    session: AsyncSession, *, library: DirectionLibrary, fields: dict[str, Any]
) -> DirectionLibrary:
    """编辑库定义（显式传 null 可清空）。P8a：库是收录配置的唯一权威源。

    - name / monthly_budget 落标量列；
    - statement/cadence/rubric/anchors/keywords/questions/goals/scope 等收录配置写入
      library.definition（ingest 从这里取数），并把有对应标量列的键镜像回列供展示；
    - 允许整体传入 ``definition`` 一次性替换。
    不再写回起源课题 project.definition（P8a 拆掉 P6 写回同步）。
    """
    if fields.get("name"):
        library.name = fields["name"]  # name 非空约束：显式 null/空串视为不改名
    if "monthly_budget" in fields:
        library.monthly_budget = fields["monthly_budget"]

    config_keys = [k for k in fields if k in _CONFIG_TO_DEFINITION]
    if "definition" in fields or config_keys:
        definition = dict(library.definition) if isinstance(library.definition, dict) else {}
        if isinstance(fields.get("definition"), dict):
            definition = dict(fields["definition"])
        for key in config_keys:
            definition[_CONFIG_TO_DEFINITION[key]] = fields[key]
        library.definition = definition or None
        # 只镜像本次触及的键对应的标量列，不动未触及列。
        touched_defn_keys = set()
        if isinstance(fields.get("definition"), dict):
            touched_defn_keys |= set(_DEFINITION_TO_COLUMN) & set(definition)
        touched_defn_keys |= {_CONFIG_TO_DEFINITION[k] for k in config_keys}
        for defn_key in touched_defn_keys:
            col = _DEFINITION_TO_COLUMN.get(defn_key)
            if col:
                setattr(library, col, definition.get(defn_key))

    await session.commit()
    await session.refresh(library)
    return library


# ---- P7：库生命周期独立（创建/删除不再绑定课题） ----


class LibraryHasTopicsError(Exception):
    """库仍有课题关联，删除需要 force=true（先解绑或确认一并解除关联）。"""


async def delete_library(
    session: AsyncSession, *, library: DirectionLibrary, force: bool = False
) -> None:
    """删库（平台 admin 专用）：论文内容池行不动；成员行/概念/策展人/课题关联行
    随库一并清除（DB ``ondelete=CASCADE``）。有课题关联且未 ``force`` → 拒绝
    （``LibraryHasTopicsError``，路由映射 409，提示先解绑或带 force 确认）。
    """
    if not force:
        linked = (
            await session.execute(
                select(TopicSourceLibrary.topic_id)
                .where(TopicSourceLibrary.library_id == library.id)
                .limit(1)
            )
        ).first()
        if linked is not None:
            raise LibraryHasTopicsError(str(library.id))
    await session.delete(library)
    await session.commit()
