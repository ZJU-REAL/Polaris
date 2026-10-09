"""方向文献库解析与成员行工具（不 import fastapi）。

P7 起课题 × 库多对多关联（``topic_source_libraries``）：课题的语料 = 关联库论文
的并集，经 ``get_source_libraries``/``get_source_library_ids`` 取数（空关联=
无语料，调用方应给空态而非报错）。``get_library_for_project`` 是历史单库解析
（起源库优先、否则第一个关联库、否则 None），逐步只供管理/ingest 路径使用——
读路径（想法生成/检索/图谱/写作引用等）应改走关联库并集。
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import (
    DirectionLibrary,
    LibraryPaper,
    TopicSourceLibrary,
)
from app.models.paper import CONCEPT_STATUS_ACTIVE, Concept, Paper, PaperWiki, paper_concepts
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)


# 自动淘汰的论文不进回收站，而是直接删掉成员行（见 papers.delete_membership_hard）。
# 成员行一没，库内去重集合就挡不住它们了；而 since_last 的扫描窗口与上次同步那天是
# 重叠的，于是同一批论文明天会再花一次 LLM。这里在库上留一份「判过且没通过」的名单
# 挡住重复。只存 id 且有上限——它是防重复的备忘，不是档案，覆盖得住重叠窗口就够。
_MAX_REJECTED_MEMORY = 5000


def rejected_paper_ids(library: DirectionLibrary) -> set[str]:
    """本库判过且相关性不达标的论文 id（不再重复送打分）。"""
    return {pid for pid in (library.ingest_state or {}).get("rejected_ids") or [] if pid}


def remember_rejected(library: DirectionLibrary, paper_ids: Iterable[str]) -> None:
    """把本轮淘汰的论文记进名单（最近的在前，超出上限的丢掉）。"""
    state = dict(library.ingest_state or {})
    previous = [pid for pid in (state.get("rejected_ids") or []) if isinstance(pid, str)]
    merged = list(dict.fromkeys([*paper_ids, *previous]))[:_MAX_REJECTED_MEMORY]
    state["rejected_ids"] = merged
    library.ingest_state = state  # 整体赋值：JSON 列不跟踪原地修改


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
    await session.execute(delete(TopicSourceLibrary).where(TopicSourceLibrary.topic_id == topic_id))
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

    跨库同一论文取确定性视角（相关性高的库优先，见 ``membership_rank``）；
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


def membership_rank(membership: LibraryPaper) -> tuple[float, str]:
    """跨库同一论文的确定性视角优先级（越小越优）：相关性分高的库优先，
    再次 library_id 稳定序。

    解读不参与排序——每篇论文只有一份解读（``paper_wikis``），换哪个库的视角都一样。"""
    return (
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


def library_paper_stmt() -> Select:
    """论文与其所在库的成员行：SELECT (Paper, LibraryPaper, project_id)。

    单用户本地应用（#842）：每个库都是这个人的，不再按人筛库。
    """
    return (
        select(Paper, LibraryPaper, DirectionLibrary.project_id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
    )


# ---- 方向库读视图 ----


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
    # 最近编译时间取解读行（论文级唯一一份）的 updated_at：库内任一论文被重编译都算
    paper_rows = await session.execute(
        select(LibraryPaper.library_id, func.count(), func.max(PaperWiki.updated_at))
        .outerjoin(PaperWiki, PaperWiki.paper_id == LibraryPaper.paper_id)
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
    # 概念是全平台一份、不属于任何库：库的概念数 = 库内论文关联到的**正式**概念去重计数
    # （候选词条不对用户可见，也就不该计入库卡片上的概念数）
    concept_rows = await session.execute(
        select(
            LibraryPaper.library_id,
            func.count(func.distinct(paper_concepts.c.concept_id)),
        )
        .join(paper_concepts, paper_concepts.c.paper_id == LibraryPaper.paper_id)
        .join(Concept, Concept.id == paper_concepts.c.concept_id)
        .where(
            LibraryPaper.library_id.in_(library_ids),
            Concept.status == CONCEPT_STATUS_ACTIVE,
        )
        .group_by(LibraryPaper.library_id)
    )
    concept_counts = {lib_id: int(count) for lib_id, count in concept_rows.all()}
    return paper_counts, last_compiled, concept_counts


def _overview_dict(
    library: DirectionLibrary,
    *,
    paper_count: int,
    concept_count: int,
    last_compiled_at: Any,
) -> dict[str, Any]:
    return {
        "id": library.id,
        "name": library.name,
        "library_kind": library.library_kind,
        "interdisciplinary_domains": library.interdisciplinary_domains,
        "discipline": library.discipline,
        "statement": library.statement,
        "cadence": library.cadence,
        "monthly_budget": library.monthly_budget,
        "definition": library_definition(library),
        "project_id": library.project_id,
        "paper_count": paper_count,
        "concept_count": concept_count,
        "last_compiled_at": last_compiled_at,
        "last_synced_at": _last_synced_of(library.ingest_state),
        "created_at": library.created_at,
        "updated_at": library.updated_at,
    }


async def _overviews(
    session: AsyncSession, libraries: Sequence[DirectionLibrary]
) -> list[dict[str, Any]]:
    paper_counts, last_compiled, concept_counts = await _library_stats(
        session, [lib.id for lib in libraries]
    )
    return [
        _overview_dict(
            lib,
            paper_count=paper_counts.get(lib.id, 0),
            concept_count=concept_counts.get(lib.id, 0),
            last_compiled_at=last_compiled.get(lib.id),
        )
        for lib in libraries
    ]


async def list_libraries_overview(session: AsyncSession) -> list[dict[str, Any]]:
    """全部方向库 + 概要统计（按创建先后）。"""
    libraries = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    return await _overviews(session, libraries)


async def library_overview(session: AsyncSession, *, library: DirectionLibrary) -> dict[str, Any]:
    """单库详情概要（同列表口径）。"""
    return (await _overviews(session, [library]))[0]


async def source_libraries_overview(
    session: AsyncSession, *, topic_id: uuid.UUID
) -> list[dict[str, Any]]:
    """课题关联库 + 概要统计（同列表口径，按关联建立时间）。"""
    return await _overviews(session, await get_source_libraries(session, topic_id))


async def default_library_sources(session: AsyncSession, discipline: str | None) -> list[str]:
    """新建文献库默认勾选的来源：学科包声明的优先，否则部署默认（#821）。"""
    from app.services import literature_settings
    from app.services.discipline_packs import pack_sources

    declared = pack_sources(discipline)
    if declared is not None:
        return declared
    settings = await literature_settings.get_settings(session)
    return [str(s) for s in settings.get("sources") or []]


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
    discipline: str | None = None,
    created_by: uuid.UUID,
) -> DirectionLibrary:
    """用户独立新建方向文献库（P10；``project_id`` 恒为 NULL——不属于任何课题，靠关联被消费）。

    新库即刻可用。创建者记为 ``submitted_by``（只是记录，不再用于任何权限判断）。

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
    # 来源一律显式落库（#821）。「没配来源」在读取侧的含义是「只用 arXiv」——那是这个
    # 字段出现之前建的库的真实行为，得原样保住；但新库不该继承这个偏向。所以在唯一的
    # 建库出口补上默认来源：学科包声明了就用它的，否则用部署默认（管理员在文献检索
    # 设置里配的来源）。读取侧的 arXiv 回退就只剩存量库会走到。
    keywords = dict(keywords or {})
    if not [s for s in keywords.get("sources") or [] if str(s).strip()]:
        keywords["sources"] = await default_library_sources(session, discipline)
    definition["keywords"] = keywords
    library = DirectionLibrary(
        name=name,
        statement=statement,
        rubric=rubric,
        anchors=anchors,
        cadence=cadence,
        definition=definition or None,  # P8a：独立库同样以 definition 为收录配置权威源
        monthly_budget=monthly_budget,
        discipline=discipline or None,  # 空串按「通用」存，不是一个叫 "" 的学科
        submitted_by=created_by,  # 归属人单列（#734 起 created_by 副本列已删）
        project_id=None,
    )
    session.add(library)
    await session.flush()
    await session.refresh(library)
    return library


async def get_managed_project(
    session: AsyncSession, *, project_id: uuid.UUID, user: User
) -> Project | None:
    """project 作用域的文献管理端点取课题：不是这个用户的课题视为不存在（返回 None）。"""
    project = await session.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        return None
    return project


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

    - name / monthly_budget / discipline 落标量列；
    - statement/cadence/rubric/anchors/keywords/questions/goals/scope 等收录配置写入
      library.definition（ingest 从这里取数），并把有对应标量列的键镜像回列供展示；
    - 允许整体传入 ``definition`` 一次性替换。
    不再写回起源课题 project.definition（P8a 拆掉 P6 写回同步）。
    """
    if fields.get("name"):
        library.name = fields["name"]  # name 非空约束：显式 null/空串视为不改名
    if "monthly_budget" in fields:
        library.monthly_budget = fields["monthly_budget"]
    if "discipline" in fields:
        # 落标量列（不进 definition）：它不是收录配置，是抽取口径的选择
        library.discipline = fields["discipline"] or None

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
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    force: bool = False,
) -> None:
    """删库。论文内容池行不动；库内论文行/概念/课题关联行随库一并清除
    （DB ``ondelete=CASCADE``）。
    有课题关联且未 ``force`` → 拒绝（``LibraryHasTopicsError``，路由映射 409，
    提示先解绑或带 force 确认）。
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
