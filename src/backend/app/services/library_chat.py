"""文献库对话（跨文献问答，不 import fastapi）。

流程：问题向量化 → 全库分段检索（pgvector；不可用时关键词降级）→
按论文分组拼编号上下文（附概念清单）→ stage=reading 流式回答，
要求用 [n] 标注引用来源、用 [[概念名]] 双链标注概念。
检索任何一步失败（表未迁移、embedding 挂了等）都不抛错：逐级降级，
最终兜底用高分论文的 TL;DR/摘要拼上下文。
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.library_direction import LibraryPaper
from app.models.paper import Concept, Paper, PaperChunk, paper_concepts
from app.models.project import Project
from app.services import chunks as chunks_service
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)

logger = logging.getLogger(__name__)

# 关联库并集读取的本地别名（跨库同一论文按确定性视角归并）
_union_member_stmt = member_papers_stmt
_dedupe = dedupe_member_rows

MAX_SOURCES = 8  # 上下文里最多引用的论文数
MAX_CHUNKS = 16  # 检索片段数上限
FALLBACK_PAPERS = 12  # 无分段时用的论文数（TL;DR/摘要）
SNIPPET_CHARS = 1500  # 单来源送入上下文的字符上限（多段拼合后截断）

LIBRARY_CHAT_SYSTEM_TEMPLATE = """\
你是文献库研究助手，基于下面从文献库中检索到的资料，帮用户做跨文献的分析、比较与综合梳理。
回答要求：
- 只依据资料回答；资料没有覆盖的，直接说明「文献库中未检索到相关内容」，不要编造；
- 引用某篇论文的内容时，在句末标注对应编号，如 [1] 或 [1][3]；
- 提到资料「概念」清单里列出的概念时，用双链 [[概念名]] 标注（只用清单里出现过的概念名，
  别的词不要加双链）；
- 当某篇论文的「配图」能直观说明你的观点时，在合适位置插入该图标记 [[fig:论文id:图号]]
  （只用资料里给出的标记，别自己编图号），插图后配一句说明它画了什么、支撑了什么结论；
- 涉及多篇论文时主动做对比与归纳（共识、分歧、演进脉络），不要逐篇罗列了事；
- 用中文回答，讲清楚、说人话。

研究方向：{statement}

检索到的资料（编号 = 论文）：
{context}
"""


@dataclass
class ChatSource:
    """一条引用来源（回给前端渲染编号 → 论文跳转）。"""

    index: int
    paper_id: str
    title: str
    year: int | None
    status: str | None = None
    relevance: float | None = None
    concepts: list[str] = field(default_factory=list)


async def _retrieve_chunks(
    session: AsyncSession,
    *,
    library_ids: list[uuid.UUID] | None,
    project_id: uuid.UUID | None,
    question: str,
    llm: LLMRouter,
    user_id: uuid.UUID | None,
    paper_ids: list[uuid.UUID] | None = None,
) -> list[tuple[PaperChunk, float]]:
    """向量检索优先（postgres + embedding 可用），否则关键词降级；任何失败都不上抛。

    典型失败：paper_chunks 表未迁移、embedding provider 挂了、向量维度不匹配——
    统一 rollback 后逐级降级（向量 → 关键词 → 空，空由调用方走论文摘要兜底）。
    传 paper_ids 时把检索限制在这些论文内（伴读引用指定文献用）。
    """
    if chunks_service.chunk_vector_search_supported(session):
        try:
            vectors = await llm.embed([question], user_id=user_id, project_id=project_id)
            rows = await chunks_service.semantic_search_chunks(
                session,
                library_ids=library_ids,
                query_vector=vectors[0],
                limit=MAX_CHUNKS,
                paper_ids=paper_ids,
            )
            if rows:
                return rows
        except NotImplementedError:
            pass
        except Exception:  # noqa: BLE001 — 检索失败降级，不打断对话
            logger.warning("chunk vector search failed; falling back to keyword", exc_info=True)
            await session.rollback()  # postgres 报错后事务已中止，先回滚
    try:
        return await chunks_service.keyword_search_chunks(
            session, library_ids=library_ids, q=question, limit=MAX_CHUNKS, paper_ids=paper_ids
        )
    except Exception:  # noqa: BLE001
        logger.warning("chunk keyword search failed; falling back to summaries", exc_info=True)
        await session.rollback()
        return []


def _figure_hints(paper: Paper) -> str:
    """把某论文的重要配图列成「标记 + 图注」，供 AI 在回答里插图（[[fig:id:idx]]）。"""
    figs = [f for f in (paper.figures or []) if f.get("important") and f.get("caption")]
    if not figs:
        return ""
    lines = [
        f"  [[fig:{paper.id}:{f['index']}]] {f.get('kind') or '图'}：{f['caption']}"
        for f in figs[:4]
    ]
    return "\n配图（可插入标记引用）：\n" + "\n".join(lines)


def _group_by_paper(
    rows: list[tuple[PaperChunk, float]], papers: dict[uuid.UUID, Paper]
) -> list[tuple[Paper, list[PaperChunk]]]:
    """按检索得分顺序把片段归到论文（保持论文首次出现的顺序），最多 MAX_SOURCES 篇。"""
    grouped: dict[uuid.UUID, list[PaperChunk]] = {}
    order: list[uuid.UUID] = []
    for chunk, _score in rows:
        if chunk.paper_id not in papers:
            continue
        if chunk.paper_id not in grouped:
            if len(order) >= MAX_SOURCES:
                continue
            grouped[chunk.paper_id] = []
            order.append(chunk.paper_id)
        grouped[chunk.paper_id].append(chunk)
    return [(papers[pid], grouped[pid]) for pid in order]


async def build_library_messages(
    session: AsyncSession,
    *,
    project: Project,
    question: str,
    history: list[tuple[str, str]],
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
) -> tuple[list[Message], list[ChatSource]]:
    """组装文献库对话消息，返回 (messages, 引用来源列表)。"""
    # 先取出所需字段：检索失败路径里的 rollback 会使 ORM 对象过期，之后再取属性会报错
    project_id = project.id
    definition = project.definition if isinstance(project.definition, dict) else {}
    statement = definition.get("statement") or project.name
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        # 课题无关联库 = 无语料：直接给「空文献库」上下文（不抓片段、不兜底）
        messages = [
            Message(
                role="system",
                content=LIBRARY_CHAT_SYSTEM_TEMPLATE.format(
                    statement=statement, context="（还没有关联任何文献库）"
                ),
            )
        ]
        messages += [Message(role=role, content=content) for role, content in history]
        messages.append(Message(role="user", content=question))
        return messages, []

    rows = await _retrieve_chunks(
        session,
        library_ids=library_ids,
        project_id=project_id,
        question=question,
        llm=llm,
        user_id=user_id,
    )
    papers: dict[uuid.UUID, Paper] = {}
    memberships: dict[uuid.UUID, LibraryPaper] = {}
    if rows:
        paper_ids = list({c.paper_id for c, _ in rows})
        found = (
            await session.execute(
                _union_member_stmt(library_ids).where(Paper.id.in_(paper_ids))
            )
        ).all()
        deduped = _dedupe(found)
        papers = {p.id: p for p, _ in deduped}
        memberships = {p.id: m for p, m in deduped}

    # (paper, 送入上下文的正文) 顺序清单：优先检索片段，否则高分论文摘要兜底
    entries: list[tuple[Paper, str]] = []
    if rows and papers:
        for paper, chunk_list in _group_by_paper(rows, papers):
            snippet = "\n…\n".join(c.text for c in chunk_list)[:SNIPPET_CHARS]
            entries.append((paper, snippet))
    else:
        fallback = _dedupe(
            (
                await session.execute(
                    _union_member_stmt(library_ids).where(
                        LibraryPaper.status.in_(("scored", "fetched", "compiled", "included"))
                    )
                )
            ).all()
        )
        fallback.sort(
            key=lambda pm: -(
                pm[1].relevance_score if pm[1].relevance_score is not None else -1e18
            )
        )
        fallback = fallback[:FALLBACK_PAPERS]
        entries = [(p, p.tldr or (p.abstract or "")[:400] or "（无摘要）") for p, _ in fallback]
        memberships |= {p.id: m for p, m in fallback}

    # 涉及论文的概念清单（回答里的 [[双链]] 只允许用这些名字，保证前端可点可跳）
    concepts_by_paper: dict[uuid.UUID, list[str]] = {}
    if entries:
        concept_rows = (
            await session.execute(
                select(paper_concepts.c.paper_id, Concept.name)
                .join(Concept, Concept.id == paper_concepts.c.concept_id)
                .where(paper_concepts.c.paper_id.in_([p.id for p, _ in entries]))
            )
        ).all()
        for paper_id, name in concept_rows:
            concepts_by_paper.setdefault(paper_id, []).append(name)

    sources: list[ChatSource] = []
    blocks: list[str] = []
    for i, (paper, body) in enumerate(entries, start=1):
        names = concepts_by_paper.get(paper.id, [])[:10]
        concept_line = f"\n概念：{'、'.join(names)}" if names else ""
        fig_line = _figure_hints(paper)
        blocks.append(
            f"[{i}] {paper.title}（{paper.year or '年份未知'}）{concept_line}{fig_line}\n{body}"
        )
        membership = memberships.get(paper.id)
        sources.append(
            ChatSource(
                index=i,
                paper_id=str(paper.id),
                title=paper.title,
                year=paper.year,
                status=membership.status if membership else None,
                relevance=membership.relevance_score if membership else None,
                concepts=names,
            )
        )

    context = "\n\n".join(blocks) or "（文献库为空）"
    messages = [
        Message(
            role="system",
            content=LIBRARY_CHAT_SYSTEM_TEMPLATE.format(statement=statement, context=context),
        )
    ]
    messages += [Message(role=role, content=content) for role, content in history]
    messages.append(Message(role="user", content=question))
    return messages, sources


async def build_scoped_messages(
    session: AsyncSession,
    *,
    statement: str | None,
    question: str,
    history: list[tuple[str, str]],
    paper_ids: list[uuid.UUID],
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> tuple[list[Message], list[ChatSource]]:
    """按「一组论文」组装文献对话消息（课题相关研究书架 / 个人库收藏）。

    与 build_library_messages 同形，但语料是显式给定的 paper_ids 集合而非某方向的
    关联库：不经 membership，检索走「纯 paper_ids、不 join 库」分支，没索引到片段的
    论文降级到 TL;DR/摘要。返回 (messages, 引用来源列表)，形状与 build_library_messages
    一致，端点可照抄 chat_with_library 的消费方式。
    """
    statement_text = statement or "（未指定研究方向）"
    if not paper_ids:
        # 空语料：直接给「还没有论文」上下文（不抓片段、不兜底）
        messages = [
            Message(
                role="system",
                content=LIBRARY_CHAT_SYSTEM_TEMPLATE.format(
                    statement=statement_text, context="（还没有收藏任何论文）"
                ),
            )
        ]
        messages += [Message(role=role, content=content) for role, content in history]
        messages.append(Message(role="user", content=question))
        return messages, []

    rows = await _retrieve_chunks(
        session,
        library_ids=None,  # 纯 paper_ids 检索，不按方向库过滤
        project_id=project_id,
        question=question,
        llm=llm,
        user_id=user_id,
        paper_ids=paper_ids,
    )
    papers: dict[uuid.UUID, Paper] = {}
    if rows:
        hit_ids = list({c.paper_id for c, _ in rows})
        found = (await session.execute(select(Paper).where(Paper.id.in_(hit_ids)))).scalars().all()
        papers = {p.id: p for p in found}

    # (paper, 送入上下文的正文)：优先检索片段，否则按给定顺序取论文摘要兜底（无 membership）
    entries: list[tuple[Paper, str]] = []
    if rows and papers:
        for paper, chunk_list in _group_by_paper(rows, papers):
            snippet = "\n…\n".join(c.text for c in chunk_list)[:SNIPPET_CHARS]
            entries.append((paper, snippet))
    else:
        fallback = (
            (
                await session.execute(
                    select(Paper).where(Paper.id.in_(paper_ids))
                )
            )
            .scalars()
            .all()
        )
        by_id = {p.id: p for p in fallback}
        # 保留调用方给定的论文顺序（书架/收藏已按新→旧排好）
        ordered = [by_id[pid] for pid in paper_ids if pid in by_id][:FALLBACK_PAPERS]
        entries = [(p, p.tldr or (p.abstract or "")[:400] or "（无摘要）") for p in ordered]

    # 涉及论文的概念清单（回答里的 [[双链]] 只允许用这些名字）
    concepts_by_paper: dict[uuid.UUID, list[str]] = {}
    if entries:
        concept_rows = (
            await session.execute(
                select(paper_concepts.c.paper_id, Concept.name)
                .join(Concept, Concept.id == paper_concepts.c.concept_id)
                .where(paper_concepts.c.paper_id.in_([p.id for p, _ in entries]))
            )
        ).all()
        for paper_id, name in concept_rows:
            concepts_by_paper.setdefault(paper_id, []).append(name)

    sources: list[ChatSource] = []
    blocks: list[str] = []
    for i, (paper, body) in enumerate(entries, start=1):
        names = concepts_by_paper.get(paper.id, [])[:10]
        concept_line = f"\n概念：{'、'.join(names)}" if names else ""
        fig_line = _figure_hints(paper)
        blocks.append(
            f"[{i}] {paper.title}（{paper.year or '年份未知'}）{concept_line}{fig_line}\n{body}"
        )
        sources.append(
            ChatSource(
                index=i,
                paper_id=str(paper.id),
                title=paper.title,
                year=paper.year,
                status=None,  # 无 membership
                relevance=None,
                concepts=names,
            )
        )

    context = "\n\n".join(blocks) or "（还没有收藏任何论文）"
    messages = [
        Message(
            role="system",
            content=LIBRARY_CHAT_SYSTEM_TEMPLATE.format(
                statement=statement_text, context=context
            ),
        )
    ]
    messages += [Message(role=role, content=content) for role, content in history]
    messages.append(Message(role="user", content=question))
    return messages, sources


async def build_reference_context(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    question: str,
    paper_ids: list[uuid.UUID],
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
) -> tuple[str, list[ChatSource]]:
    """阅读伴读用：给「用户额外选中的其他文献」拼参考上下文，返回 (编号上下文, 来源列表)。

    对选中论文按问题检索相关片段（向量→关键词→空，同库对话的降级路径），
    某篇没检索到片段时回退到它的 TL;DR/摘要——保证每篇选中的文献都出现在上下文里。
    只纳入属于本项目的论文（跨项目引用被过滤）。返回空串表示没有可用的参考文献。
    """
    if not paper_ids:
        return "", []
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return "", []
    found = _dedupe(
        (
            await session.execute(
                _union_member_stmt(library_ids).where(Paper.id.in_(paper_ids))
            )
        ).all()
    )
    papers = {p.id: p for p, _ in found}
    memberships = {p.id: m for p, m in found}
    # 保留用户的选择顺序，并滤掉不属于本项目的
    ordered = [papers[pid] for pid in paper_ids if pid in papers]
    if not ordered:
        return "", []

    rows = await _retrieve_chunks(
        session,
        library_ids=library_ids,
        project_id=project_id,
        question=question,
        llm=llm,
        user_id=user_id,
        paper_ids=[p.id for p in ordered],
    )
    grouped: dict[uuid.UUID, list[PaperChunk]] = {}
    if rows:
        grouped = {paper.id: cl for paper, cl in _group_by_paper(rows, papers)}

    sources: list[ChatSource] = []
    blocks: list[str] = []
    for i, paper in enumerate(ordered, start=1):
        chunk_list = grouped.get(paper.id)
        if chunk_list:
            body = ("\n…\n".join(c.text for c in chunk_list))[:SNIPPET_CHARS]
        else:
            body = paper.tldr or (paper.abstract or "")[:400] or "（无摘要）"
        blocks.append(f"[{i}] {paper.title}（{paper.year or '年份未知'}）\n{body}")
        membership = memberships.get(paper.id)
        sources.append(
            ChatSource(
                index=i,
                paper_id=str(paper.id),
                title=paper.title,
                year=paper.year,
                status=membership.status if membership else None,
                relevance=membership.relevance_score if membership else None,
            )
        )
    return "\n\n".join(blocks), sources
