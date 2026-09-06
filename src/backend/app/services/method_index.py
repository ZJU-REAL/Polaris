"""方法库的双轴索引与类比检索（#663，设计报告 §11 燃料 3）。

一篇论文的 method@1 抽取产物（services/extraction/schemas.py）拆出两根语义轴：
purpose（要达成什么）与 mechanism（靠什么机制达成），各自 embed 一条向量存进
method_vectors。为什么拆两轴而不是整卡一条向量：方法库最值钱的问法是「同目的
异机制」——我想达成的目标别人怎么达成的、有没有换个思路的做法。整卡一条向量
只能回答「像不像」，回答不了「目的像但路子不像」。

检索语义（search_methods）：
- same_purpose：purpose 轴近邻，纯粹的「找同类做法」；
- different_mechanism：先取 purpose 轴近邻圈出「目的相近」的池子，再按 mechanism
  轴与查询的相似度**升序**排——目的最像、机制离得最远的排最前，即类比检索。

向量存储沿用向量侧表口径（models/vectors.py）：不带 library_id（论文是共享内容池，
库的边界检索时经 library_papers 圈定）、带 space（换嵌入模型走既有的空间切换机制）、
Python 侧算余弦（候选集 = 单库有方法卡的论文，量级几百，顺序扫描在 sqlite/postgres
上行为一致，测试也因此可用 fake embedding 确定性断言排序）。

增量：enrich 钩子与 backfill CLI 在 method@1 产物落表后调 refresh_paper_method_index
同步刷索引。嵌入不可用（NotImplementedError）时由调用方按既有降级路径处理；
检索侧降级为关键词匹配（mode_used 如实上报），不给一个看着正常实则乱序的结果。
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.models.vectors import MethodVector

logger = logging.getLogger(__name__)

METHOD_SCHEMA_ID = "method"

#: 双轴：进向量的两个 text 字段。list 字段（baseline/dataset）与 protocol 只做
#: 展示与关键词降级，不进向量——它们是复现要素，不是语义轴。
AXES = ("purpose", "mechanism")

SEARCH_MODES = ("same_purpose", "different_mechanism")

#: different_mechanism 的 purpose 近邻池子大小下限：池子太小时「目的相近」这个
#: 前提名存实亡，排序退化成纯 mechanism 距离
_DIFFERENT_POOL_MIN = 20


def _axis_texts(payload: dict[str, Any]) -> dict[str, str]:
    """method@1 payload 里实际抽到的轴文本（缺键/空串不进结果）。"""
    out: dict[str, str] = {}
    for axis in AXES:
        value = payload.get(axis)
        if isinstance(value, str) and value.strip():
            out[axis] = value.strip()
    return out


async def refresh_paper_method_index(
    session: AsyncSession,
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> bool:
    """按当前 method@1 产物重建这篇论文的双轴向量，返回是否写了任何行。

    调用方负责 commit。没有产物 / 两根轴都没抽到时**清掉**存量向量而不是留着——
    重抽可能把某根轴抽没了，幽灵向量会让这篇论文继续在该轴命中。嵌入不可用时
    抛 NotImplementedError（调用方按 skipped 处理），此时不动存量。
    """
    row = await session.scalar(
        select(PaperExtraction).where(
            PaperExtraction.paper_id == paper.id,
            PaperExtraction.schema_id == METHOD_SCHEMA_ID,
        )
    )
    texts = _axis_texts(row.payload) if row is not None else {}

    from app.services.embedding import embed_documents, upsert_method_vector

    wrote = False
    if texts:
        axes = list(texts)
        vectors, space = await embed_documents(
            session,
            [texts[a] for a in axes],
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
        )
        for axis, vector in zip(axes, vectors, strict=True):
            await upsert_method_vector(session, paper.id, axis, vector, space)
            wrote = True

    # 没抽到的轴清掉（全部空间：旧空间的幽灵在切回旧模型时同样是错的）
    stale = [a for a in AXES if a not in texts]
    if stale:
        await session.execute(
            delete(MethodVector).where(
                MethodVector.paper_id == paper.id, MethodVector.axis.in_(stale)
            )
        )
    return wrote


async def _method_rows(
    session: AsyncSession, library_id: uuid.UUID
) -> list[tuple[Paper, PaperExtraction]]:
    """库内（相关性达标、不含回收站）有 method@1 产物的论文，按入库时间倒序。"""
    from app.services.papers import PAPER_STATUS_GROUPS

    stmt = (
        select(Paper, PaperExtraction)
        .join(PaperExtraction, PaperExtraction.paper_id == Paper.id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(
            PaperExtraction.schema_id == METHOD_SCHEMA_ID,
            LibraryPaper.library_id == library_id,
            LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
        )
        .order_by(LibraryPaper.created_at.desc())
    )
    return [(paper, ext) for paper, ext in (await session.execute(stmt)).all()]


def _card(paper: Paper, ext: PaperExtraction) -> dict[str, Any]:
    payload = ext.payload or {}
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "purpose": payload.get("purpose"),
        "mechanism": payload.get("mechanism"),
        "baseline": payload.get("baseline") or [],
        "dataset": payload.get("dataset") or [],
        "protocol": payload.get("protocol"),
        "similarity": None,
        "mechanism_similarity": None,
    }


async def list_methods(
    session: AsyncSession, library_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    """方法库列表：库内已抽取论文的五元组卡（无查询时的默认视图）。"""
    rows = await _method_rows(session, library_id)
    return [_card(paper, ext) for paper, ext in rows[:limit]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _keyword_overlap(query_tokens: set[str], text: str | None) -> int:
    if not text:
        return 0
    return len(query_tokens & _tokens(text))


async def search_methods(
    session: AsyncSession,
    library_id: uuid.UUID,
    query: str,
    *,
    mode: str = "same_purpose",
    limit: int = 20,
    user_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """方法检索，返回 (卡片列表, mode_used)。

    语义路径要求候选在激活空间下有所需轴的向量：same_purpose 只要 purpose 轴，
    different_mechanism 两轴都要（缺 mechanism 向量的论文没法排远近，如实排除而
    不是编一个位置）。嵌入不可用时降级为确定性的关键词匹配（mode_used="keyword"）：
    same_purpose 按 purpose 文本的查询词命中数降序；different_mechanism 先按 purpose
    命中数圈池子，再按 mechanism 文本的命中数**升序**——语义方向与向量路径一致。
    """
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown method search mode: {mode!r}")
    rows = await _method_rows(session, library_id)
    if not rows:
        return [], "semantic"

    try:
        from app.services.embedding import embed_query

        query_vector, space = await embed_query(
            session, query, user_id=user_id, library_id=library_id
        )
    except NotImplementedError:
        return _keyword_search(rows, query, mode=mode, limit=limit), "keyword"

    vec_rows = (
        await session.execute(
            select(MethodVector).where(
                MethodVector.paper_id.in_([paper.id for paper, _ in rows]),
                MethodVector.space == space.key,
            )
        )
    ).scalars()
    vectors: dict[tuple[uuid.UUID, str], list[float]] = {
        (v.paper_id, v.axis): list(v.embedding) for v in vec_rows
    }

    scored: list[dict[str, Any]] = []
    for paper, ext in rows:
        purpose_vec = vectors.get((paper.id, "purpose"))
        if purpose_vec is None:
            continue
        card = _card(paper, ext)
        card["similarity"] = _cosine(query_vector, purpose_vec)
        mechanism_vec = vectors.get((paper.id, "mechanism"))
        if mechanism_vec is not None:
            card["mechanism_similarity"] = _cosine(query_vector, mechanism_vec)
        elif mode == "different_mechanism":
            continue  # 没有机制轴就没法排「机制远近」
        scored.append(card)

    # tie-break 用 paper_id 字符串：同分时排序仍然确定
    scored.sort(key=lambda c: (-c["similarity"], str(c["paper_id"])))
    if mode == "same_purpose":
        return scored[:limit], "semantic"

    pool = scored[: max(limit, _DIFFERENT_POOL_MIN)]
    pool.sort(
        key=lambda c: (c["mechanism_similarity"], -c["similarity"], str(c["paper_id"]))
    )
    return pool[:limit], "semantic"


def _keyword_search(
    rows: list[tuple[Paper, PaperExtraction]],
    query: str,
    *,
    mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    """确定性关键词降级：查询分词与轴文本的命中数（无 LLM、无向量）。"""
    query_tokens = _tokens(query)
    cards: list[tuple[int, int, dict[str, Any]]] = []
    for paper, ext in rows:
        card = _card(paper, ext)
        p_hits = _keyword_overlap(query_tokens, card["purpose"])
        m_hits = _keyword_overlap(query_tokens, card["mechanism"])
        cards.append((p_hits, m_hits, card))

    if mode == "same_purpose":
        cards.sort(key=lambda item: (-item[0], str(item[2]["paper_id"])))
        return [c for _, _, c in cards[:limit]]

    # different_mechanism：purpose 命中数圈池子，池内 mechanism 命中数升序
    cards.sort(key=lambda item: (-item[0], str(item[2]["paper_id"])))
    pool = cards[: max(limit, _DIFFERENT_POOL_MIN)]
    pool.sort(key=lambda item: (item[1], -item[0], str(item[2]["paper_id"])))
    return [c for _, _, c in pool[:limit]]
