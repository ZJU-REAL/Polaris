"""概念库业务逻辑：wikilink 解析、slug、单篇上链、列表/详情查询（不 import fastapi）。

概念是论文级的（``concepts`` 无归属库、slug 全局唯一）：谁编译出这个词条都用同一行。
「某个库有哪些概念」不存，一律按 ``library_concept_ids`` 推导——库的论文 →
关联概念 → 去重。

入库把关（两道）：
① 转正门槛（:data:`CONCEPT_PROMOTION_MIN_PAPERS`，确定性）——新概念先记 candidate，
   被第 2 篇论文用到才复核转正。只有一篇论文用的词绝大多数是那篇自己起的名字
   （benchmark / 数据集 / 模型代号），留在候选里不进概念库；**定义也推迟到转正时才生成**，
   不为一次性专有名词花 token。关联（``paper_concepts``）照常建，只是候选不对用户可见。
② 有效性判定（判断性任务，走 LLM）——转正时本来就要调模型写定义，顺带让它判断这个名字
   到底是不是个学术概念。图表引用（``fig:1``）、编号（``12``）、半句话这类判为无效，
   落 ``rejected`` 终态，不进概念库也不再复判。门槛拦不住 ``fig:1`` 这种被几十篇论文
   各标一次的垃圾，正好由这一步兜住；判定失败则降级为「照常转正」，不阻塞。
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, delete, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.base import utcnow
from app.models.library_direction import LibraryPaper
from app.models.paper import (
    CONCEPT_STATUS_ACTIVE,
    CONCEPT_STATUS_CANDIDATE,
    CONCEPT_STATUS_REJECTED,
    Concept,
    Paper,
    PaperWiki,
    paper_concepts,
)

logger = logging.getLogger(__name__)

CONCEPT_DEF_SYSTEM_PROMPT = """\
你是 Librarian，为研究 wiki 的概念词条做两件事：判断这个名字是不是一个有意义的学术概念，
是的话再给出一句话中文定义与类别。
下列情况一律判为无效（valid=false，definition 留空）：
- 图表 / 公式 / 章节引用：fig:1、Figure 2、table:3、eq:4、Section 5；
- 编号或纯数字、纯符号：1、12、35、---；
- 不成词的片段、半句话、整句叙述，或明显是解读正文被误标进双链的内容；
- 与学术无关的通用词。
判断只看名字本身：拿不准但确实是领域术语（方法、架构、任务/问题、指标、数据集、模型族）
的，判为有效。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"concepts": [{"name": "原样回抄的名字", "valid": true, "definition": "一句话定义", \
"category": "method|architecture|methodology|problem|metric|dataset|other"}]}
"""

# [[概念名]] / [[概念名|别名]] / [[概念名#锚点]]
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:[|#][^\[\]]*)?\]\]")

CONCEPT_CATEGORIES = (
    "method",
    "architecture",
    "methodology",
    "problem",
    "metric",
    "dataset",
    "other",
)

_SLUG_STRIP_RE = re.compile(r"[^\w一-鿿]+", re.UNICODE)

# 转正门槛：被这么多篇论文引用的概念才进概念库。
# 1 篇 = 多半是这篇论文自己起的名字（benchmark / 数据集 / 模型代号），不是跨论文复现的概念。
CONCEPT_PROMOTION_MIN_PAPERS = 2

# 概念定义按批调用 LLM：一次塞几百个会让响应被 max_tokens 截断 → JSON 解析失败 → 整批占位。
_DEF_BATCH_SIZE = 40
# 自动上链步骤每次最多回填的占位概念数（手动补建端点不设上限）
_AUTO_BACKFILL_CAP = 60
_PLACEHOLDER_SUFFIX = "（定义待补充）"


def placeholder_definition(name: str) -> str:
    """新概念还没拿到 LLM 定义时的占位文案。"""
    return f"{name}{_PLACEHOLDER_SUFFIX}"


def is_placeholder_definition(text: str | None) -> bool:
    """该定义是否仍是占位（批量截断/失败留下的「…（定义待补充）」）。"""
    return bool(text) and text.endswith(_PLACEHOLDER_SUFFIX)


def extract_wikilinks(markdown: str) -> list[str]:
    """解析 [[..]] 双链，返回去重（保序）的概念名列表。

    跳过 ``![[...]]`` 嵌入标记（如图文 wiki 的 ``![[fig:N]]``，docs/api-lit.md §6.6）；
    感叹号与括号之间夹了空格的 ``! [[fig:3]]`` 也算嵌入标记（模型偶尔这么写）。
    「这名字配不配当概念」不在这里判——那是判断题，交给转正时的有效性判定
    （见 :func:`promote_ready_concepts`），这里只做确定性的解析。
    """
    markdown = markdown or ""
    seen: dict[str, None] = {}
    for match in WIKILINK_RE.finditer(markdown):
        before = markdown[: match.start()].rstrip(" \t")
        if before.endswith("!"):
            continue  # 嵌入（图片）标记不是概念双链
        name = match.group(1).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def wiki_slug(name: str) -> str:
    """概念/论文 slug：保留中英文字符，其余折叠为 '-'；空则退回内容 hash。"""
    slug = _SLUG_STRIP_RE.sub("-", name.strip().lower()).strip("-")
    return slug or hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


async def _free_slug(session: AsyncSession, name: str) -> str:
    """给新概念取一个没被占用的 slug（全局唯一；撞了加随机后缀）。"""
    slug = wiki_slug(name)
    taken = (
        (await session.execute(select(Concept.slug).where(Concept.slug.like(f"{slug}%"))))
        .scalars()
        .all()
    )
    if slug not in set(taken):
        return slug
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def normalize_category(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in CONCEPT_CATEGORIES else "other"


def library_concept_ids(library_ids: Sequence[uuid.UUID] | Select) -> Select:
    """「这些库有哪些**正式**概念」的 concept_id 子查询：库的论文 → 关联概念（去重由 IN 保证）。

    候选概念（还没被第 2 篇论文用到）不算数——所有以库/课题为作用域的读路径
    （概念列表、搜索、导出、图谱统计、agent 工具、idea 种子校验）都经此收口。

    ``library_ids`` 也接受一个**子查询**（如「用户够得着的全部库」），这样全局搜索
    不必绕开这个收口自己拼一遍——绕开的那份迟早会漏掉「候选概念不算数」这一条。
    """
    scope = library_ids if isinstance(library_ids, Select) else list(library_ids)
    return (
        select(paper_concepts.c.concept_id)
        .join(LibraryPaper, LibraryPaper.paper_id == paper_concepts.c.paper_id)
        .join(Concept, Concept.id == paper_concepts.c.concept_id)
        .where(
            LibraryPaper.library_id.in_(scope),
            Concept.status == CONCEPT_STATUS_ACTIVE,
        )
    )


async def list_concepts(
    session: AsyncSession,
    *,
    library_ids: Sequence[uuid.UUID],
    category: str | None = None,
    q: str | None = None,
) -> list[tuple[Concept, int]]:
    """方向库并集概念列表（附 paper_count）。传单库时给 ``[library_id]``；课题作用域
    传关联库并集。空列表 = 无语料，返回空。

    paper_count 是**全平台**引用数（概念本身是全平台一份），与详情页口径一致。"""
    if not library_ids:
        return []
    paper_count = func.count(paper_concepts.c.paper_id).label("paper_count")
    stmt = (
        select(Concept, paper_count)
        .outerjoin(paper_concepts, paper_concepts.c.concept_id == Concept.id)
        .where(Concept.id.in_(library_concept_ids(library_ids)))
        .group_by(Concept.id)
        .order_by(paper_count.desc(), Concept.name)
    )
    if category:
        stmt = stmt.where(Concept.category == category)
    if q:
        stmt = stmt.where(Concept.name.ilike(f"%{q}%"))
    return [(concept, int(count)) for concept, count in (await session.execute(stmt)).all()]


async def find_by_name(session: AsyncSession, name: str) -> list[Concept]:
    """平台级按名字查概念（[[双链]] 解析用）：名字精确匹配（忽略大小写），
    再退一步按 slug 匹配（吃掉空格/连接符的写法差异）。

    概念全平台一份，正常至多命中一条；空结果 = 还没入库。候选概念按「还没入库」处理
    ——它确实还不是正式词条，等第二篇论文引用它就自动出现，不需要人工干预。
    """
    needle = name.strip()
    if not needle:
        return []
    stmt = select(Concept).where(
        Concept.status == CONCEPT_STATUS_ACTIVE,
        or_(func.lower(Concept.name) == needle.lower(), Concept.slug == wiki_slug(needle)),
    )
    return list((await session.execute(stmt)).scalars().all())


async def paper_count_of(session: AsyncSession, concept_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(paper_concepts.c.concept_id == concept_id)
    return int((await session.execute(stmt)).scalar_one())


async def papers_of_concept(
    session: AsyncSession, concept_id: uuid.UUID, *, library_id: uuid.UUID | None = None
) -> list[Paper]:
    """关联到这个概念的论文。

    ``library_id`` 给出时只列该库内的（从库的上下文点进概念时用）；不给就是全平台
    ——词条本身永远是同一份，按库变化的只有这份论文清单。"""
    stmt = (
        select(Paper)
        .join(paper_concepts, paper_concepts.c.paper_id == Paper.id)
        .where(paper_concepts.c.concept_id == concept_id)
        .order_by(Paper.published_at.desc().nulls_last(), Paper.created_at.desc())
    )
    if library_id is not None:
        stmt = stmt.where(
            Paper.id.in_(select(LibraryPaper.paper_id).where(LibraryPaper.library_id == library_id))
        )
    return list((await session.execute(stmt)).scalars().all())


async def related_concepts(
    session: AsyncSession, concept: Concept, limit: int = 10
) -> list[tuple[Concept, int]]:
    """相关概念 = 与本概念共现于同一论文的正式概念，按共现次数取 top N（候选不出现）。"""
    pc_self = paper_concepts.alias("pc_self")
    pc_other = paper_concepts.alias("pc_other")
    cooccur = func.count().label("cooccur")
    stmt = (
        select(Concept, cooccur)
        .join(pc_other, pc_other.c.concept_id == Concept.id)
        .join(pc_self, pc_self.c.paper_id == pc_other.c.paper_id)
        .where(
            pc_self.c.concept_id == concept.id,
            Concept.id != concept.id,
            Concept.status == CONCEPT_STATUS_ACTIVE,
        )
        .group_by(Concept.id)
        .order_by(cooccur.desc(), Concept.name)
        .limit(limit)
    )
    return [(c, int(n)) for c, n in (await session.execute(stmt)).all()]


async def fetch_concept_definitions(
    llm: LLMRouter,
    names: list[str],
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, dict[str, str]]:
    """向 LLM 要「是不是概念」的判定 + 一句话定义与类别；分批调用避免响应截断，
    失败的批重试一次；仍失败则该批返回不到结果，调用方按「判不了就照常放行」降级。"""
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(names), _DEF_BATCH_SIZE):
        chunk = names[i : i + _DEF_BATCH_SIZE]
        got = await _fetch_definitions_batch(
            llm,
            chunk,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
        if not got:
            # 高负载下的偶发超时/限流：整批失败会让本批全落占位，重试一次能救回大多数
            got = await _fetch_definitions_batch(
                llm,
                chunk,
                user_id=user_id,
                project_id=project_id,
                library_id=library_id,
                voyage_id=voyage_id,
            )
        out.update(got)
    return out


async def _fetch_definitions_batch(
    llm: LLMRouter,
    names: list[str],
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, dict[str, str]]:
    """单批（≤_DEF_BATCH_SIZE 个）判定 + 定义调用；失败返回空 dict（调用方兜底）。"""
    if not names:
        return {}
    try:
        result = await llm.complete(
            "extract",
            [
                Message(role="system", content=CONCEPT_DEF_SYSTEM_PROMPT),
                Message(role="user", content="概念列表：" + json.dumps(names, ensure_ascii=False)),
            ],
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
        start = result.content.find("{")
        end = result.content.rfind("}")
        data = json.loads(result.content[start : end + 1])
        return {
            str(item["name"]): item
            for item in data.get("concepts", [])
            if isinstance(item, dict) and item.get("name")
        }
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 定义失败用占位，不阻塞上链
        logger.warning("concept definition fetch failed", exc_info=True)
        return {}


async def _new_candidate_concept(session: AsyncSession, name: str) -> Concept:
    """建新词条：一律先记候选——不对用户可见，也**不生成定义**（转正时才花那次调用）。"""
    concept = Concept(
        name=name, slug=await _free_slug(session, name), status=CONCEPT_STATUS_CANDIDATE
    )
    session.add(concept)
    return concept


async def promote_ready_concepts(
    session: AsyncSession,
    *,
    concept_ids: Sequence[uuid.UUID],
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[list[Concept], list[Concept]]:
    """把够门槛的候选概念复核转正（每次上链后调用），返回 (转正的, 判为无效的)。

    门槛 = 关联论文数 ≥ :data:`CONCEPT_PROMOTION_MIN_PAPERS`（引用计数不分论文 status，
    与孤儿清理同口径）。够门槛的才向 LLM 要「是不是概念」的判定 + 一句话定义——89% 的
    概念只被一篇论文用到，给它们提前花这次调用是纯浪费。判为无效的落 rejected 终态
    （``fig:1`` 这种被几十篇论文各标一次的垃圾就死在这里）；判不了（无 LLM / 调用失败）
    则照常转正、定义留占位，由后续复核自愈，不阻塞。不 commit，由调用方提交。
    """
    ids = list(dict.fromkeys(concept_ids))
    if not ids:
        return [], []
    counts = (
        await session.execute(
            select(paper_concepts.c.concept_id, func.count())
            .where(paper_concepts.c.concept_id.in_(ids))
            .group_by(paper_concepts.c.concept_id)
        )
    ).all()
    ready = [cid for cid, n in counts if int(n) >= CONCEPT_PROMOTION_MIN_PAPERS]
    if not ready:
        return [], []
    rows = list(
        (
            await session.execute(
                select(Concept).where(
                    Concept.id.in_(ready), Concept.status == CONCEPT_STATUS_CANDIDATE
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return [], []

    verdicts: dict[str, dict[str, Any]] = {}
    if llm is not None:
        verdicts = await fetch_concept_definitions(
            llm,
            [c.name for c in rows],
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    promoted, rejected = [], []
    for concept in rows:
        if _apply_verdict(concept, verdicts.get(concept.name)):
            promoted.append(concept)
        else:
            rejected.append(concept)
    await session.flush()
    return promoted, rejected


def _normalize_generated_concept_names(values: Any) -> list[str]:
    """清洗简报模型返回的概念名，保序去重并限制单篇概念数量。"""
    if not isinstance(values, list):
        return []
    names: dict[str, None] = {}
    for value in values:
        name = " ".join(str(value or "").strip().split())
        if name.startswith("[[") and name.endswith("]]"):
            name = name[2:-2].strip()
        if name.lower().startswith("concepts/"):
            name = name[len("concepts/") :].strip()
        name = name.split("|", 1)[0].split("#", 1)[0].strip()
        if not name or len(name) > 255:
            continue
        names.setdefault(name, None)
        if len(names) >= 6:
            break
    return list(names)


async def link_generated_paper_concepts(
    session: AsyncSession,
    *,
    concepts_by_paper: dict[str | uuid.UUID, list[Any]],
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """把每日简报模型提取的概念落库并关联论文。

    这条路径补足「今日论文已评分、但尚未精读编译」时的概念关联。它只增补关联，不清除
    解读正文建立的既有关系；新名字仍遵循统一的候选/转正治理：至少被两篇论文复用并通过
    有效性复核后才成为可点击的正式概念。返回值只包含正式概念，供简报生成 wikilink。
    """
    normalized: dict[uuid.UUID, list[str]] = {}
    for raw_paper_id, values in concepts_by_paper.items():
        try:
            paper_id = uuid.UUID(str(raw_paper_id))
        except (TypeError, ValueError):
            continue
        normalized[paper_id] = _normalize_generated_concept_names(values)

    names = sorted({name for paper_names in normalized.values() for name in paper_names})
    if not normalized or not names:
        return (
            {str(paper_id): [] for paper_id in normalized},
            {
                "concepts_created": 0,
                "links_created": 0,
                "concepts_promoted": 0,
                "concepts_rejected": 0,
                "active_concepts": 0,
            },
        )

    existing = list(
        (await session.execute(select(Concept).where(Concept.name.in_(names)))).scalars().all()
    )
    existing_by_name = {concept.name: concept for concept in existing}
    by_name = {
        name: concept
        for name, concept in existing_by_name.items()
        if concept.status != CONCEPT_STATUS_REJECTED
    }
    created = 0
    for name in names:
        # 已判为无效的名字保持终态，不重新创建或关联。
        if name in existing_by_name:
            continue
        by_name[name] = await _new_candidate_concept(session, name)
        created += 1
    await session.flush()

    paper_ids = list(normalized)
    existing_pairs = {
        (paper_id, concept_id)
        for paper_id, concept_id in (
            await session.execute(
                select(paper_concepts.c.paper_id, paper_concepts.c.concept_id).where(
                    paper_concepts.c.paper_id.in_(paper_ids)
                )
            )
        ).all()
    }
    linked = 0
    linked_concept_ids: set[uuid.UUID] = set()
    for paper_id, paper_names in normalized.items():
        for name in paper_names:
            concept = by_name.get(name)
            if concept is None:
                continue
            linked_concept_ids.add(concept.id)
            pair = (paper_id, concept.id)
            if pair in existing_pairs:
                continue
            await session.execute(
                insert(paper_concepts).values(paper_id=paper_id, concept_id=concept.id)
            )
            existing_pairs.add(pair)
            linked += 1

    # 先释放写事务，再让概念复核模型通过独立连接记账，避免 SQLite 写锁互相等待。
    await session.commit()
    promoted, rejected = await promote_ready_concepts(
        session,
        concept_ids=list(linked_concept_ids),
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    await session.commit()

    active_rows = (
        await session.execute(
            select(paper_concepts.c.paper_id, Concept.name)
            .join(Concept, Concept.id == paper_concepts.c.concept_id)
            .where(
                paper_concepts.c.paper_id.in_(paper_ids),
                Concept.status == CONCEPT_STATUS_ACTIVE,
                Concept.name.in_(names),
            )
            .order_by(Concept.name)
        )
    ).all()
    active_by_paper = {str(paper_id): [] for paper_id in paper_ids}
    for paper_id, name in active_rows:
        if name in normalized[paper_id]:
            active_by_paper[str(paper_id)].append(name)
    active_names = {name for _, name in active_rows}
    return active_by_paper, {
        "concepts_created": created,
        "links_created": linked,
        "concepts_promoted": len(promoted),
        "concepts_rejected": len(rejected),
        "active_concepts": len(active_names),
    }


def _apply_verdict(concept: Concept, meta: dict[str, Any] | None) -> bool:
    """把一条判定结果写到词条上，返回它是否留在概念库（True=有效）。

    ``meta`` 为空 = 这次没判成（无 LLM / 调用失败 / 模型漏回这个名字）：按「放行」处理
    ——宁可暂时多留一个词条，也不能让一次超时把真概念误杀成终态。定义留占位，
    ``validated_at`` 不落，下次同步会再判一次。
    """
    if meta is not None and meta.get("valid") is False:
        concept.status = CONCEPT_STATUS_REJECTED
        concept.validated_at = utcnow()  # 终态，判过就不再复判
        return False
    if meta is not None:
        concept.validated_at = utcnow()  # 判过了：下次同步不必再为它花一次
    definition = str((meta or {}).get("definition") or "")
    if definition and not is_placeholder_definition(definition):
        concept.definition = definition
        concept.category = normalize_category((meta or {}).get("category"))
    elif not concept.definition:
        concept.definition = placeholder_definition(concept.name)
        concept.category = normalize_category(concept.category)
    concept.status = CONCEPT_STATUS_ACTIVE
    return True


async def delete_orphan_concepts(
    session: AsyncSession,
    *,
    candidate_ids: set[uuid.UUID] | None = None,
) -> int:
    """删除零引用概念（paper_concepts 计数，含回收站论文的引用），返回删除数。

    概念是全平台一份，孤儿判定也就是全平台口径：任何论文都不再引用才删。
    ``candidate_ids`` 给出时只在这些概念里找孤儿（单篇同步后的定向检查）；
    不给时全表扫描。不 commit，由调用方提交。
    """
    if candidate_ids is not None and not candidate_ids:
        return 0
    stmt = delete(Concept).where(~exists().where(paper_concepts.c.concept_id == Concept.id))
    if candidate_ids is not None:
        stmt = stmt.where(Concept.id.in_(list(candidate_ids)))
    result = await session.execute(stmt.execution_options(synchronize_session="fetch"))
    return int(result.rowcount or 0)


async def _remove_stale_paper_links(
    session: AsyncSession, paper_id: uuid.UUID, keep_concept_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """删除该论文上不在 ``keep_concept_ids`` 里的概念关联，返回被解除的概念 id。

    关联全部来自这篇论文解读正文里的 [[双链]]（论文级唯一一份），正文没有的就是陈旧的。
    """
    stale = {
        cid
        for (cid,) in (
            await session.execute(
                select(paper_concepts.c.concept_id).where(paper_concepts.c.paper_id == paper_id)
            )
        ).all()
        if cid not in keep_concept_ids
    }
    if stale:
        await session.execute(
            delete(paper_concepts).where(
                paper_concepts.c.paper_id == paper_id,
                paper_concepts.c.concept_id.in_(list(stale)),
            )
        )
    return stale


async def link_all_paper_concepts(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    backfill: bool = False,
) -> tuple[dict[str, Any], list[Paper]]:
    """全库概念上链（voyage wiki.link_concepts 步骤与手动补建端点共用）：

    对项目内全部已编译论文（compiled/included 且已有解读）抽 [[双链]]，
    缺失概念建**候选**词条（不生成定义）、补齐 paper_concepts 关联；已存在的概念与
    关联跳过，幂等可重跑。

    建链完成后做同步收尾：①清除扫描范围内每篇论文正文已不再引用的陈旧关联
    （正文为空的论文跳过，不误删）；②删除项目内所有零引用概念（引用计数不分
    论文 status，回收站论文的引用也算）；③把够门槛（≥2 篇论文引用）的候选概念转正，
    **这时才**给它们生成定义。

    占位概念回填：``backfill=True``（手动补建端点）回填全部占位概念；
    ``backfill=False``（voyage 自动步骤）也做**有上限**的回填——每次最多取
    ``_AUTO_BACKFILL_CAP`` 个最老的占位重新要定义，让偶发失败随每日同步自愈,
    又不至于在占位堆积时每天重刷几百个。
    返回 (统计 dict, 涉及的论文列表)；本函数自行 commit。
    project_id 仅用于 LLM 用量记账归属。
    """
    rows = (
        await session.execute(
            select(Paper, LibraryPaper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .join(PaperWiki, PaperWiki.paper_id == Paper.id)  # 有解读的才有双链可抽
            .where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.status.in_(("compiled", "included")),
            )
        )
    ).all()
    papers = [paper for paper, _ in rows]
    wiki_by_paper = {paper.id: paper.wiki_content for paper, _ in rows}
    links_by_paper = {pid: extract_wikilinks(wiki or "") for pid, wiki in wiki_by_paper.items()}
    all_names = sorted({name for names in links_by_paper.values() for name in names})

    # 概念全平台一份：按名字查全表（别的库先建过同名词条，这里直接复用）
    existing = (
        (await session.execute(select(Concept).where(Concept.name.in_(all_names)))).scalars().all()
    )
    by_name = {c.name: c for c in existing}
    new_names = [n for n in all_names if n not in by_name]

    created = 0
    for name in new_names:
        by_name[name] = await _new_candidate_concept(session, name)
        created += 1
    await session.flush()

    existing_pairs = (
        {
            (pid, cid)
            for pid, cid in (
                await session.execute(
                    select(paper_concepts.c.paper_id, paper_concepts.c.concept_id).where(
                        paper_concepts.c.paper_id.in_(list(links_by_paper))
                    )
                )
            ).all()
        }
        if links_by_paper
        else set()
    )
    new_links = 0
    for paper_id, names in links_by_paper.items():
        for name in names:
            concept = by_name.get(name)
            if concept is None or (paper_id, concept.id) in existing_pairs:
                continue
            await session.execute(
                insert(paper_concepts).values(paper_id=paper_id, concept_id=concept.id)
            )
            existing_pairs.add((paper_id, concept.id))
            new_links += 1

    # 同步收尾①：清除每篇论文正文已不再引用的陈旧关联（正文为空的不动，防误删）
    links_removed = 0
    pairs_by_paper: dict[uuid.UUID, set[uuid.UUID]] = {}
    for pid, cid in existing_pairs:
        pairs_by_paper.setdefault(pid, set()).add(cid)
    for paper in papers:
        if not wiki_by_paper.get(paper.id):
            continue
        target_ids = {
            by_name[name].id for name in links_by_paper.get(paper.id, []) if name in by_name
        }
        stale = pairs_by_paper.get(paper.id, set()) - target_ids
        if stale:
            await session.execute(
                delete(paper_concepts).where(
                    paper_concepts.c.paper_id == paper.id,
                    paper_concepts.c.concept_id.in_(list(stale)),
                )
            )
            links_removed += len(stale)
    # 同步收尾②：删除零引用概念（含回收站论文的引用都算数，只删真孤儿）
    concepts_removed = await delete_orphan_concepts(session)
    # 先落上面的建链/清理，再做转正：转正要调 LLM 拿定义，那期间不该压着写事务
    # （记账写入用的是另一条连接，压着事务在 sqlite 上直接死锁）
    await session.commit()
    # 同步收尾③：转正——本轮扫到的论文所关联的候选概念里，够 2 篇的转正并补定义。
    # 取全部关联（不止本轮新建的）：陈旧关联刚被清掉、或上一轮转正因故没跑到的，
    # 都能在下次同步里自愈。
    linked_ids = (
        (
            await session.execute(
                select(paper_concepts.c.concept_id)
                .where(paper_concepts.c.paper_id.in_(list(links_by_paper)))
                .distinct()
            )
        )
        .scalars()
        .all()
        if links_by_paper
        else []
    )
    promoted, rejected = await promote_ready_concepts(
        session,
        concept_ids=list(linked_ids),
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    # 同步收尾④：复核库内正式概念——补齐占位定义 + 判定「还没判过」的老词条
    # （门槛改造之前存量转正的那批 validated_at 为空，正是 fig:1 这类垃圾的藏身处）
    backfilled, rejected_old = await review_active_concepts(
        session,
        library_id=library_id,
        extra_names=all_names,
        llm=llm,
        limit=None if backfill else _AUTO_BACKFILL_CAP,
        user_id=user_id,
        project_id=project_id,
        voyage_id=voyage_id,
    )
    await session.commit()

    rejected += rejected_old
    stats = {
        "papers": len(papers),
        "concepts_created": created,
        "new_concepts": new_names,
        "links_created": new_links,
        "concepts_backfilled": backfilled,
        "links_removed": links_removed,
        "concepts_removed": concepts_removed,
        "concepts_promoted": len(promoted),
        "promoted_concepts": sorted(c.name for c in promoted),
        "concepts_rejected": len(rejected),
        "rejected_concepts": sorted(c.name for c in rejected),
    }
    return stats, list(papers)


async def review_active_concepts(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    extra_names: Sequence[str] = (),
    llm: LLMRouter | None = None,
    limit: int | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[int, list[Concept]]:
    """复核库内的正式概念，返回 (补齐定义数, 判为无效而下架的概念)。

    两种要复核的：①定义还是占位的（此前调用失败/截断留下的）；②``validated_at`` 为空的
    ——门槛改造之前存量就转正的那批，从没被判过是不是概念，``fig:1`` 这类高引用垃圾就藏
    在里面。两者共用同一次调用（本来就要问定义，顺带问有效性），判为无效的落 rejected。

    ``limit`` 给出时只取最老的那批（voyage 自动步骤用，免得一次刷几百个）；手动补建传
    None 做全量。范围是本库论文关联到的概念 + ``extra_names``（本轮正文引用到的同名词条）
    ——别的库自己那摊由它们自己的同步复核。不 commit，由调用方提交。
    """
    scoped = (
        (
            await session.execute(
                select(Concept).where(
                    Concept.status == CONCEPT_STATUS_ACTIVE,
                    or_(
                        Concept.id.in_(library_concept_ids([library_id])),
                        Concept.name.in_(list(extra_names)) if extra_names else False,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    pending = sorted(
        (c for c in scoped if c.validated_at is None or is_placeholder_definition(c.definition)),
        key=lambda c: (c.created_at is None, c.created_at or c.name),
    )
    if limit is not None:
        pending = pending[:limit]
    if not pending or llm is None:
        return 0, []

    verdicts = await fetch_concept_definitions(
        llm,
        [c.name for c in pending],
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,  # 概念定义是库侧编译成本，记方向库账（P6）
        voyage_id=voyage_id,
    )
    backfilled = 0
    rejected: list[Concept] = []
    for concept in pending:
        was_placeholder = is_placeholder_definition(concept.definition)
        if not _apply_verdict(concept, verdicts.get(concept.name)):
            rejected.append(concept)
        elif was_placeholder and not is_placeholder_definition(concept.definition):
            backfilled += 1
    await session.flush()
    return backfilled, rejected


async def link_paper_concepts(
    session: AsyncSession,
    paper: Paper,
    membership: LibraryPaper | None = None,
    *,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """单篇概念上链（手动编译/重编译/每日推送编译后调用，docs/api-lit.md §6.6）——同步语义：

    从这篇论文的解读正文抽 [[双链]] → 缺失概念建**候选**词条（全平台一份，已有同名的
    直接复用；候选不生成定义）→ 建关联；再删除该论文上新正文已不引用的陈旧关联，被解除
    关联且再无任何论文引用的概念一并删除；最后把够门槛（≥2 篇论文引用）的候选送去复核，
    模型判定确实是概念的转正并生成定义，判为无效的落 rejected。
    正文为空/None 时直接返回，不做任何删除（防误删）。
    返回 (新建概念数, 新建关联数)——新建的都是候选，用户还看不到，直到第二篇论文引用它。
    调用方无需再 commit（本函数自行提交）。
    membership / project_id 仅用于 LLM 用量记账归属：概念与库无关，不属于任何库的论文
    （每日推送里直接编译的）照样上链，费用记平台级（library_id 空）+ 触发的人。
    """
    if not paper.wiki_content:
        return 0, 0
    # 仅用于 LLM 用量记账（P6）：从库里发起的记那个库的账，池级路径记平台级
    library_id = membership.library_id if membership is not None else None
    names = extract_wikilinks(paper.wiki_content)
    existing = (
        (await session.execute(select(Concept).where(Concept.name.in_(names)))).scalars().all()
    )
    by_name = {c.name: c for c in existing}
    new_names = [n for n in names if n not in by_name]

    created = 0
    for name in new_names:
        by_name[name] = await _new_candidate_concept(session, name)
        created += 1
    await session.flush()

    existing_pairs = {
        cid
        for (cid,) in (
            await session.execute(
                select(paper_concepts.c.concept_id).where(paper_concepts.c.paper_id == paper.id)
            )
        ).all()
    }
    linked = 0
    for name in names:
        concept = by_name.get(name)
        if concept is None or concept.id in existing_pairs:
            continue
        await session.execute(
            insert(paper_concepts).values(paper_id=paper.id, concept_id=concept.id)
        )
        existing_pairs.add(concept.id)
        linked += 1

    # 同步收尾：删除新正文已不引用的陈旧关联；被解除关联且再无引用的概念删词条
    target_ids = {by_name[name].id for name in names if name in by_name}
    stale = await _remove_stale_paper_links(session, paper.id, target_ids)
    await delete_orphan_concepts(session, candidate_ids=stale)
    # 先落建链/清理再转正：转正要调 LLM 判定 + 拿定义，那期间不该压着写事务
    await session.commit()
    # 转正：这篇引用到的候选概念里，够 2 篇论文的送去复核，有效的转正并生成定义
    await promote_ready_concepts(
        session,
        concept_ids=list(target_ids),
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
    )
    await session.commit()
    return created, linked
