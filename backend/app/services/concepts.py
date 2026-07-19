"""概念库业务逻辑：wikilink 解析、slug、单篇上链、列表/详情查询（不 import fastapi）。"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.paper import Concept, Paper, paper_concepts

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


def normalize_category(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in CONCEPT_CATEGORIES else "other"


async def list_concepts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    category: str | None = None,
    q: str | None = None,
) -> list[tuple[Concept, int]]:
    """项目概念列表（附 paper_count）。"""
    paper_count = func.count(paper_concepts.c.paper_id).label("paper_count")
    stmt = (
        select(Concept, paper_count)
        .outerjoin(paper_concepts, paper_concepts.c.concept_id == Concept.id)
        .where(Concept.project_id == project_id)
        .group_by(Concept.id)
        .order_by(paper_count.desc(), Concept.name)
    )
    if category:
        stmt = stmt.where(Concept.category == category)
    if q:
        stmt = stmt.where(Concept.name.ilike(f"%{q}%"))
    return [(concept, int(count)) for concept, count in (await session.execute(stmt)).all()]


async def paper_count_of(session: AsyncSession, concept_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(paper_concepts.c.concept_id == concept_id)
    return int((await session.execute(stmt)).scalar_one())


async def papers_of_concept(session: AsyncSession, concept_id: uuid.UUID) -> list[Paper]:
    stmt = (
        select(Paper)
        .join(paper_concepts, paper_concepts.c.paper_id == Paper.id)
        .where(paper_concepts.c.concept_id == concept_id)
        .order_by(Paper.published_at.desc().nulls_last(), Paper.created_at.desc())
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
    voyage_id: uuid.UUID | None = None,
) -> dict[str, dict[str, str]]:
    """向 LLM 要概念的一句话定义与类别；分批调用避免响应截断，失败的批用占位兜底。"""
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(names), _DEF_BATCH_SIZE):
        out.update(
            await _fetch_definitions_batch(
                llm,
                names[i : i + _DEF_BATCH_SIZE],
                user_id=user_id,
                project_id=project_id,
                voyage_id=voyage_id,
            )
        )
    return out


async def _fetch_definitions_batch(
    llm: LLMRouter,
    names: list[str],
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, dict[str, str]]:
    """单批（≤_DEF_BATCH_SIZE 个）定义调用；失败返回空 dict（调用方用占位定义兜底）。"""
    if not names:
        return {}
    try:
        result = await llm.complete(
            "librarian",
            [
                Message(role="system", content=CONCEPT_DEF_SYSTEM_PROMPT),
                Message(role="user", content="概念列表：" + json.dumps(names, ensure_ascii=False)),
            ],
            user_id=user_id,
            project_id=project_id,
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


async def link_all_paper_concepts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    backfill: bool = False,
) -> tuple[dict[str, Any], list[Paper]]:
    """全库概念上链（voyage wiki.link_concepts 步骤与手动补建端点共用）：

    对项目内全部已编译论文（compiled/included 且有 wiki_content）抽 [[双链]]，
    缺失概念建词条（新概念分批调 LLM 拿定义，失败占位）、补齐 paper_concepts
    关联；已存在的概念与关联跳过，幂等可重跑。

    ``backfill=True`` 时，把此前批量截断/失败留下的占位概念（「…（定义待补充）」）
    一并重新要定义并更新——仅供手动补建端点使用，voyage 自动步骤不开启以免每次重刷。
    返回 (统计 dict, 涉及的论文列表)；本函数自行 commit。
    """
    papers = (
        (
            await session.execute(
                select(Paper).where(
                    Paper.project_id == project_id,
                    Paper.status.in_(("compiled", "included")),
                    Paper.wiki_content.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    links_by_paper = {p.id: extract_wikilinks(p.wiki_content or "") for p in papers}
    all_names = sorted({name for names in links_by_paper.values() for name in names})

    existing = (
        (await session.execute(select(Concept).where(Concept.project_id == project_id)))
        .scalars()
        .all()
    )
    by_name = {c.name: c for c in existing}
    slugs = {c.slug for c in existing}
    new_names = [n for n in all_names if n not in by_name]
    # 回填：此前批量截断/失败留下的占位概念，本次一并重新要定义（仅手动补建时开启）
    placeholder_concepts = (
        [c for c in existing if is_placeholder_definition(c.definition)] if backfill else []
    )
    names_to_define = new_names + [c.name for c in placeholder_concepts]

    definitions: dict[str, dict[str, str]] = {}
    if names_to_define and llm is not None:
        definitions = await fetch_concept_definitions(
            llm, names_to_define, user_id=user_id, project_id=project_id, voyage_id=voyage_id
        )

    created = 0
    for name in new_names:
        slug = wiki_slug(name)
        if slug in slugs:
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        slugs.add(slug)
        meta = definitions.get(name) or {}
        concept = Concept(
            project_id=project_id,
            name=name,
            slug=slug,
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
    await session.commit()

    stats = {
        "papers": len(papers),
        "concepts_created": created,
        "new_concepts": new_names,
        "links_created": new_links,
        "concepts_backfilled": backfilled,
    }
    return stats, list(papers)


async def link_paper_concepts(
    session: AsyncSession,
    paper: Paper,
    *,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """单篇概念上链（手动编译后调用，docs/api-lit.md §6.6）：

    从 wiki_content 抽 [[双链]] → 缺失概念建词条（LLM 定义，失败占位）→ 建关联。
    返回 (新建概念数, 新建关联数)。调用方无需再 commit（本函数自行提交）。
    """
    names = extract_wikilinks(paper.wiki_content or "")
    if not names:
        return 0, 0
    existing = (
        (await session.execute(select(Concept).where(Concept.project_id == paper.project_id)))
        .scalars()
        .all()
    )
    by_name = {c.name: c for c in existing}
    slugs = {c.slug for c in existing}
    new_names = [n for n in names if n not in by_name]

    definitions: dict[str, dict[str, str]] = {}
    if new_names and llm is not None:
        definitions = await fetch_concept_definitions(
            llm, new_names, user_id=user_id, project_id=paper.project_id
        )

    created = 0
    for name in new_names:
        slug = wiki_slug(name)
        if slug in slugs:
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        slugs.add(slug)
        meta = definitions.get(name) or {}
        concept = Concept(
            project_id=paper.project_id,
            name=name,
            slug=slug,
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
    await session.commit()
    return created, linked
