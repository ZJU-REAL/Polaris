"""概念库业务逻辑：wikilink 解析、slug、单篇上链、列表/详情查询（不 import fastapi）。

概念是论文级的（``concepts`` 无归属库、slug 全局唯一）：谁编译出这个词条都用同一行。
「某个库有哪些概念」不存，一律按 ``library_concept_ids`` 推导——库的论文 →
关联概念 → 去重。
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
from app.models.library_direction import LibraryPaper
from app.models.paper import Concept, Paper, PaperWiki, paper_concepts

logger = logging.getLogger(__name__)

CONCEPT_DEF_SYSTEM_PROMPT = """\
你是 Librarian，为研究 wiki 的新概念词条给出一句话中文定义与类别。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"concepts": [{"name": "概念名", "definition": "一句话定义", \
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

    跳过 ``![[...]]`` 嵌入标记（如图文 wiki 的 ``![[fig:N]]``，docs/api-lit.md §6.6）。
    """
    markdown = markdown or ""
    seen: dict[str, None] = {}
    for match in WIKILINK_RE.finditer(markdown):
        if match.start() > 0 and markdown[match.start() - 1] == "!":
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
        await session.execute(select(Concept.slug).where(Concept.slug.like(f"{slug}%")))
    ).scalars().all()
    if slug not in set(taken):
        return slug
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def normalize_category(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in CONCEPT_CATEGORIES else "other"


def library_concept_ids(library_ids: Sequence[uuid.UUID]) -> Select:
    """「这些库有哪些概念」的 concept_id 子查询：库的论文 → 关联概念（去重由 IN 保证）。"""
    return (
        select(paper_concepts.c.concept_id)
        .join(LibraryPaper, LibraryPaper.paper_id == paper_concepts.c.paper_id)
        .where(LibraryPaper.library_id.in_(list(library_ids)))
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

    概念全平台一份，正常至多命中一条；空结果 = 还没入库。
    """
    needle = name.strip()
    if not needle:
        return []
    stmt = select(Concept).where(
        or_(func.lower(Concept.name) == needle.lower(), Concept.slug == wiki_slug(needle))
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
            Paper.id.in_(
                select(LibraryPaper.paper_id).where(LibraryPaper.library_id == library_id)
            )
        )
    return list((await session.execute(stmt)).scalars().all())


async def related_concepts(
    session: AsyncSession, concept: Concept, limit: int = 10
) -> list[tuple[Concept, int]]:
    """相关概念 = 与本概念共现于同一论文的概念，按共现次数取 top N。"""
    pc_self = paper_concepts.alias("pc_self")
    pc_other = paper_concepts.alias("pc_other")
    cooccur = func.count().label("cooccur")
    stmt = (
        select(Concept, cooccur)
        .join(pc_other, pc_other.c.concept_id == Concept.id)
        .join(pc_self, pc_self.c.paper_id == pc_other.c.paper_id)
        .where(pc_self.c.concept_id == concept.id, Concept.id != concept.id)
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
    """向 LLM 要概念的一句话定义与类别；分批调用避免响应截断，失败的批重试一次后用占位兜底。"""
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
    """单批（≤_DEF_BATCH_SIZE 个）定义调用；失败返回空 dict（调用方用占位定义兜底）。"""
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
                select(paper_concepts.c.concept_id).where(
                    paper_concepts.c.paper_id == paper_id
                )
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
    缺失概念建词条（新概念分批调 LLM 拿定义，失败占位）、补齐 paper_concepts
    关联；已存在的概念与关联跳过，幂等可重跑。

    建链完成后做同步收尾：①清除扫描范围内每篇论文正文已不再引用的陈旧关联
    （正文为空的论文跳过，不误删）；②删除项目内所有零引用概念（引用计数不分
    论文 status，回收站论文的引用也算）。

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
        (await session.execute(select(Concept).where(Concept.name.in_(all_names))))
        .scalars()
        .all()
    )
    by_name = {c.name: c for c in existing}
    new_names = [n for n in all_names if n not in by_name]
    # 占位回填的范围：本库论文已关联到的概念 + 本轮正文引用到的同名概念（马上就要关联
    # 上）。别的库自己那摊占位由它们自己的同步补，不在这里连带刷。
    scoped = (
        (
            await session.execute(
                select(Concept).where(
                    or_(
                        Concept.id.in_(library_concept_ids([library_id])),
                        Concept.name.in_(all_names),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    # 回填：定义调用失败留下的占位概念重新要定义。手动补建全量;自动步骤取最老的一批
    # (上限 _AUTO_BACKFILL_CAP),让偶发失败随每日同步自愈而不至于每天重刷几百个。
    all_placeholders = sorted(
        (c for c in scoped if is_placeholder_definition(c.definition)),
        key=lambda c: (c.created_at is None, c.created_at or c.name),
    )
    placeholder_concepts = all_placeholders if backfill else all_placeholders[:_AUTO_BACKFILL_CAP]
    names_to_define = new_names + [c.name for c in placeholder_concepts]

    definitions: dict[str, dict[str, str]] = {}
    if names_to_define and llm is not None:
        definitions = await fetch_concept_definitions(
            llm,
            names_to_define,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,  # 概念定义是库侧编译成本，记方向库账（P6）
            voyage_id=voyage_id,
        )

    created = 0
    for name in new_names:
        meta = definitions.get(name) or {}
        concept = Concept(
            name=name,
            slug=await _free_slug(session, name),
            definition=str(meta.get("definition") or placeholder_definition(name)),
            category=normalize_category(meta.get("category")),
        )
        session.add(concept)
        by_name[name] = concept
        created += 1

    backfilled = 0
    for concept in placeholder_concepts:
        meta = definitions.get(concept.name) or {}
        definition = str(meta["definition"]) if meta.get("definition") else None
        if definition and not is_placeholder_definition(definition):
            concept.definition = definition
            concept.category = normalize_category(meta.get("category"))
            backfilled += 1
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
    await session.commit()

    stats = {
        "papers": len(papers),
        "concepts_created": created,
        "new_concepts": new_names,
        "links_created": new_links,
        "concepts_backfilled": backfilled,
        "links_removed": links_removed,
        "concepts_removed": concepts_removed,
    }
    return stats, list(papers)


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

    从这篇论文的解读正文抽 [[双链]] → 缺失概念建词条（全平台一份，已有同名的直接复用；
    LLM 定义，失败占位）→ 建关联；再删除该论文上新正文已不引用的陈旧关联，被解除关联
    且再无任何论文引用的概念一并删除。正文为空/None 时直接返回，不做任何删除（防误删）。
    返回 (新建概念数, 新建关联数)。调用方无需再 commit（本函数自行提交）。
    membership / project_id 仅用于 LLM 用量记账归属：概念与库无关，不属于任何库的论文
    （每日推送里直接编译的）照样上链，费用记平台级（library_id 空）+ 触发的人。
    """
    if not paper.wiki_content:
        return 0, 0
    # 仅用于 LLM 用量记账（P6）：从库里发起的记那个库的账，池级路径记平台级
    library_id = membership.library_id if membership is not None else None
    names = extract_wikilinks(paper.wiki_content)
    existing = (
        (await session.execute(select(Concept).where(Concept.name.in_(names))))
        .scalars()
        .all()
    )
    by_name = {c.name: c for c in existing}
    new_names = [n for n in names if n not in by_name]

    definitions: dict[str, dict[str, str]] = {}
    if new_names and llm is not None:
        definitions = await fetch_concept_definitions(
            llm, new_names, user_id=user_id, project_id=project_id, library_id=library_id
        )

    created = 0
    for name in new_names:
        meta = definitions.get(name) or {}
        concept = Concept(
            name=name,
            slug=await _free_slug(session, name),
            definition=str(meta.get("definition") or placeholder_definition(name)),
            category=normalize_category(meta.get("category")),
        )
        session.add(concept)
        by_name[name] = concept
        created += 1
    await session.flush()

    existing_pairs = {
        cid
        for (cid,) in (
            await session.execute(
                select(paper_concepts.c.concept_id).where(
                    paper_concepts.c.paper_id == paper.id
                )
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
    await session.commit()
    return created, linked
