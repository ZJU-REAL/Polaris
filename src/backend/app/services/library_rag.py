"""库级 agentic RAG（#644，设计报告 §10 ⑤层，PaperQA2 四件套；不 import fastapi）。

与 ``library_chat.py``（单趟检索 + 流式闲聊）不同，这里是一条**证据先行**的问答
流水线，四个环节可单测、可重放：

1. 迭代查询扩展：首轮按问题检索 top-k，``rag_expand`` 环节基于命中提出 ≤3 条
   补充查询，再确定性执行（最多 ``max_rounds`` 轮）——一次检索抓不全的多跳问题
   靠补充查询补齐；
2. 重排 + 摘要：合并去重后 ``rag_rerank`` 环节一次调用批量打分排序，top-n 各配
   一句上下文摘要；
3. 引文图补召回：对强命中论文沿 paper_citations 边（引用 + 被引）把库内邻居的
   高分片段拉进候选（确定性，无 LLM）——语义检索按相似度召回，引文边补上
   「被它引用/引用它」这层结构性相关；
4. 证据先行作答：``rag_answer`` 环节**只看见证据集**，要求逐句挂 [paper_id]
   引用；后处理校验引用 id ∈ 证据集，越界即剥离并标注——引用只能指向真实证据，
   结构上杜绝编造引用。

小库直通：库内片段总量按 字符数/4 估算 <200k token 时跳过整条检索流水线，
全部片段直接喂给 ``rag_answer``——检索的意义在于「装不下才挑」，装得下就别挑，
挑就有漏。

检索底座复用 chunks.py：postgres+embedding 可用走向量，否则关键词降级；首轮与
扩展轮共用同一条降级路径（降级后 ``via`` 仍按轮次记 vector/expansion——它标的是
「这条证据怎么来的」，不是用了哪个检索引擎）。
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperChunk
from app.models.paper_citation import PaperCitation
from app.services import chunks as chunks_service
from app.services.embedding import embed_query
from app.services.papers import PAPER_STATUS_GROUPS

logger = logging.getLogger(__name__)

TOP_K = 8  # 每轮检索取回的片段数
MAX_EXPANSION_QUERIES = 3  # 每轮扩展最多补几条查询
RERANK_POOL = 24  # 送重排的候选上限（重排是一次 LLM 调用，输入要有上界）
TOP_N_EVIDENCE = 8  # 重排后进入作答的证据数
SNIPPET_CHARS = 360  # 证据卡片上的片段截断
RERANK_DOC_CHARS = 600  # 送重排的单条文本截断
# 小库直通阈值（token，按 字符数/4 估算）。测试里把它 monkeypatch 成 0 可强制走
# 检索流水线——正常测试库都只有几个片段，永远命中直通分支。
DIRECT_TOKEN_BUDGET = 200_000
CITATION_SEED_PAPERS = 3  # 沿引文边扩展的强命中论文数
MAX_CITATION_NEIGHBORS = 4  # 最多拉入几个引文邻居
CITATION_NEIGHBOR_CHUNKS = 2  # 每个邻居拉入的高分片段数

_WORD_RE = re.compile(r"[\w一-鿿]+")
# 回答里的引用标记：[<paper uuid>]。作答环节要求模型只用这种形态引用。
_CITE_RE = re.compile(
    r"\[([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]"
)

_EXPAND_SYSTEM = """\
POLARIS_RAG_EXPAND
你是文献检索的查询扩展器。输入 JSON 里是用户问题和首轮检索命中的片段。
基于命中内容判断还缺哪些角度，提出最多 3 条**互补**的检索查询（换措辞、换切入点、
补上位/下位概念），不要重复原问题。
只输出 JSON：{"queries": ["...", "..."]}；没有值得补的角度就输出 {"queries": []}。"""

_RERANK_SYSTEM = """\
POLARIS_RAG_RERANK
你是检索结果重排器。输入 JSON 里是用户问题和候选片段（各带 id）。
对每条片段按「回答该问题的有用程度」给 0~1 分，并配一句它讲了什么的摘要。
只输出 JSON：{"items": [{"id": <原样回传>, "score": 0到1, "summary": "..."}]}。"""

_ANSWER_SYSTEM = """\
POLARIS_RAG_ANSWER
你是文献库问答助手。输入 JSON 里是问题和检索到的证据集（每条带 paper_id）。
回答要求：
- 只依据证据回答；证据没有覆盖的，直接说明「文献库中未检索到相关内容」，不要编造；
- 每个论断句末尾标注它依据的证据来源，格式是 [paper_id]（方括号里放证据里的
  paper_id 原文，如 [123e4567-e89b-12d3-a456-426614174000]；多来源就连写多个）；
- 只允许引用证据集里出现过的 paper_id；
- 用中文回答，讲清楚、说人话。"""


@dataclass(slots=True)
class _Candidate:
    """一条候选证据。``order`` 是进入候选集的先后（重排同分时的确定性 tie-break）。"""

    chunk: PaperChunk
    score: float
    via: str  # vector | expansion | citation | direct
    order: int


def _json_payload(content: str) -> dict:
    """从模型输出里抠出第一个 {...} 并解析；失败返回 {}（调用方按解析失败降级）。"""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _search_round(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    query: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> list[tuple[PaperChunk, float]]:
    """执行一轮检索：向量优先，关键词降级，任何失败不上抛（与 library_chat 同路径）。"""
    if chunks_service.chunk_vector_search_supported(session):
        try:
            vector, space = await embed_query(
                session, query, user_id=user_id, project_id=project_id
            )
            rows = await chunks_service.semantic_search_chunks(
                session,
                library_ids=[library_id],
                query_vector=vector,
                space=space,
                limit=TOP_K,
            )
            if rows:
                return rows
        except NotImplementedError:
            pass
        except Exception:  # noqa: BLE001 — 检索失败降级关键词，不打断问答
            logger.warning("rag vector search failed; falling back to keyword", exc_info=True)
            await session.rollback()  # postgres 报错后事务已中止，先回滚
    try:
        return await chunks_service.keyword_search_chunks(
            session, library_ids=[library_id], q=query, limit=TOP_K
        )
    except Exception:  # noqa: BLE001
        logger.warning("rag keyword search failed; returning no hits", exc_info=True)
        await session.rollback()
        return []


# D3 假设管线（services/hypothesis_pipeline.py，#648）复用的确定性检索原语别名：
# 一轮检索 = 向量优先、关键词降级、失败不上抛。公开命名以示这是稳定的复用面；
# answer() 完整流水线里的 LLM 环节（查询扩展/重排/作答）**不在**此复用面之内——
# 接地与查新要的是「同样输入永远同样证据集」，不能混进判断性环节。
retrieve_round = _search_round


def _merge_hits(
    candidates: dict[uuid.UUID, _Candidate],
    rows: list[tuple[PaperChunk, float]],
    *,
    via: str,
) -> None:
    """把一轮命中并入候选集：chunk 去重，重复命中保留更高分；``via`` 记首次来源。"""
    for chunk, score in rows:
        existing = candidates.get(chunk.id)
        if existing is None:
            candidates[chunk.id] = _Candidate(
                chunk=chunk, score=float(score), via=via, order=len(candidates)
            )
        elif float(score) > existing.score:
            existing.score = float(score)


async def _expand_queries(
    llm: LLMRouter,
    *,
    question: str,
    executed: list[str],
    candidates: dict[uuid.UUID, "_Candidate"],
    papers: dict[uuid.UUID, Paper],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> list[str]:
    """rag_expand 环节：基于当前命中提出 ≤3 条补充查询（去重、剔除已执行过的）。"""
    ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.order))[:TOP_K]
    hits = [
        {
            "paper_id": str(c.chunk.paper_id),
            "title": (papers[c.chunk.paper_id].title if c.chunk.paper_id in papers else ""),
            "snippet": c.chunk.text[:300],
        }
        for c in ranked
    ]
    result = await llm.complete(
        "rag_expand",
        [
            Message(role="system", content=_EXPAND_SYSTEM),
            Message(
                role="user",
                content=json.dumps({"question": question, "hits": hits}, ensure_ascii=False),
            ),
        ],
        temperature=0.0,
        user_id=user_id,
        project_id=project_id,
    )
    seen = {q.strip() for q in executed}
    out: list[str] = []
    for raw in _json_payload(result.content).get("queries") or []:
        if not isinstance(raw, str):
            continue
        query = raw.strip()
        if query and query not in seen:
            seen.add(query)
            out.append(query)
        if len(out) >= MAX_EXPANSION_QUERIES:
            break
    return out


async def _citation_supplement(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    question: str,
    candidates: dict[uuid.UUID, "_Candidate"],
) -> None:
    """引文图补召回（确定性，无 LLM）：强命中论文的库内引文邻居贡献高分片段。

    「邻居的高分片段」按问题分词的命中数打分（与关键词降级检索同一口径），全都
    不沾边时退回开头片段——邻居是因为**引文关系**被拉进来的，正文措辞不同不该
    让它出局。
    """
    if not candidates:
        return
    ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.order))
    seeds: list[uuid.UUID] = []
    for cand in ranked:
        if cand.chunk.paper_id not in seeds:
            seeds.append(cand.chunk.paper_id)
        if len(seeds) >= CITATION_SEED_PAPERS:
            break
    # 库内成员集合（回收站/候选不算）：邻居必须在库里，问答的语料边界不能被引文边突破
    member_ids = set(
        (
            await session.execute(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
            )
        )
        .scalars()
        .all()
    )
    edges = (
        (
            await session.execute(
                select(PaperCitation)
                .where(
                    PaperCitation.citing_paper_id.in_(seeds)
                    | PaperCitation.cited_paper_id.in_(seeds)
                )
                .order_by(PaperCitation.citing_paper_id, PaperCitation.ref_index)
            )
        )
        .scalars()
        .all()
    )
    covered = {c.chunk.paper_id for c in candidates.values()}
    neighbors: list[uuid.UUID] = []
    for edge in edges:
        for pid in (edge.cited_paper_id, edge.citing_paper_id):
            if (
                pid is not None
                and pid not in seeds
                and pid in member_ids
                and pid not in covered
                and pid not in neighbors
            ):
                neighbors.append(pid)
    neighbors = neighbors[:MAX_CITATION_NEIGHBORS]
    if not neighbors:
        return
    terms = [t for t in _WORD_RE.findall(question.lower()) if len(t) >= 2][:8]
    chunks = (
        (
            await session.execute(
                select(PaperChunk)
                .where(PaperChunk.paper_id.in_(neighbors))
                .order_by(PaperChunk.paper_id, PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    by_paper: dict[uuid.UUID, list[PaperChunk]] = {}
    for chunk in chunks:
        by_paper.setdefault(chunk.paper_id, []).append(chunk)

    def term_score(chunk: PaperChunk) -> float:
        lowered = chunk.text.lower()
        return float(sum(1 for t in terms if t in lowered))

    for pid in neighbors:
        rows = by_paper.get(pid) or []
        rows.sort(key=lambda c: (-term_score(c), c.seq))
        for chunk in rows[:CITATION_NEIGHBOR_CHUNKS]:
            # 归一化到 0~1，和检索得分同量纲；关键词全不沾边给个非零底分，
            # 保证「因引文关系入选」的片段不会在重排解析失败的兜底排序里垫底消失
            score = (term_score(chunk) / len(terms)) if terms else 0.0
            _merge_hits(candidates, [(chunk, max(score, 0.05))], via="citation")


async def _rerank(
    llm: LLMRouter,
    *,
    question: str,
    ordered: list["_Candidate"],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> tuple[list["_Candidate"], dict[uuid.UUID, float], dict[uuid.UUID, str]]:
    """rag_rerank 环节：批量打分排序 + 逐条摘要（同一次调用）。

    返回 (重排后的候选, chunk_id→重排分, chunk_id→摘要)。输出解析失败时保持
    原始检索得分排序（确定性兜底，不重试）。
    """
    items = [
        {"id": i, "paper_id": str(c.chunk.paper_id), "text": c.chunk.text[:RERANK_DOC_CHARS]}
        for i, c in enumerate(ordered)
    ]
    result = await llm.complete(
        "rag_rerank",
        [
            Message(role="system", content=_RERANK_SYSTEM),
            Message(
                role="user",
                content=json.dumps({"question": question, "items": items}, ensure_ascii=False),
            ),
        ],
        temperature=0.0,
        user_id=user_id,
        project_id=project_id,
    )
    parsed: dict[int, tuple[float, str]] = {}
    for item in _json_payload(result.content).get("items") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        idx = int(item["id"])
        if not 0 <= idx < len(ordered):
            continue
        raw_score = item.get("score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
        parsed[idx] = (min(1.0, max(0.0, score)), str(item.get("summary") or ""))
    if not parsed:
        logger.warning("rag_rerank output unparseable; keeping retrieval order")
        return ordered, {}, {}
    # 打过分的按 (分数, 原序) 排；漏打分的按原序垫后——一条都不丢
    scored = sorted(
        (i for i in range(len(ordered)) if i in parsed), key=lambda i: (-parsed[i][0], i)
    )
    missing = [i for i in range(len(ordered)) if i not in parsed]
    reranked = [ordered[i] for i in scored + missing]
    rerank_scores = {ordered[i].chunk.id: parsed[i][0] for i in parsed}
    summaries = {ordered[i].chunk.id: parsed[i][1] for i in parsed if parsed[i][1]}
    return reranked, rerank_scores, summaries


def _strip_invalid_citations(answer_text: str, allowed: set[str]) -> tuple[str, int]:
    """剥离不在证据集内的 [paper_id] 引用，返回 (清理后的回答, 剥离数)。

    引用校验是这条流水线「结构上杜绝编造引用」的最后一环：模型给的每个引用要么
    指向真实证据，要么被摘掉并明说——不存在「看起来有出处」的第三态。
    """
    stripped = 0

    def _replace(m: re.Match) -> str:
        nonlocal stripped
        if m.group(1).lower() in allowed:
            return m.group(0)
        stripped += 1
        return ""

    cleaned = _CITE_RE.sub(_replace, answer_text)
    if stripped:
        cleaned = cleaned.rstrip() + f"\n\n（注：已剥离 {stripped} 处不在证据集内的引用。）"
    return cleaned, stripped


async def _paper_rows(
    session: AsyncSession, paper_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Paper]:
    if not paper_ids:
        return {}
    rows = (
        (await session.execute(select(Paper).where(Paper.id.in_(paper_ids)))).scalars().all()
    )
    return {p.id: p for p in rows}


def _evidence_entry(
    cand: "_Candidate",
    papers: dict[uuid.UUID, Paper],
    *,
    score: float | None,
) -> dict:
    paper = papers.get(cand.chunk.paper_id)
    return {
        "paper_id": str(cand.chunk.paper_id),
        "chunk_id": str(cand.chunk.id),
        "title": paper.title if paper is not None else "",
        "snippet": cand.chunk.text[:SNIPPET_CHARS],
        "score": score,
        "via": cand.via,
    }


async def _answer_from_evidence(
    llm: LLMRouter,
    *,
    question: str,
    evidence: list[dict],
    summaries_by_chunk: dict[str, str],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> str:
    """rag_answer 环节：只喂证据集作答，再做引用越界校验。"""
    payload = {
        "question": question,
        "evidence": [
            {
                "paper_id": e["paper_id"],
                "title": e["title"],
                "snippet": e["snippet"],
                "summary": summaries_by_chunk.get(e["chunk_id"] or "", ""),
            }
            for e in evidence
        ],
    }
    result = await llm.complete(
        "rag_answer",
        [
            Message(role="system", content=_ANSWER_SYSTEM),
            Message(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ],
        temperature=0.0,
        user_id=user_id,
        project_id=project_id,
    )
    allowed = {e["paper_id"].lower() for e in evidence}
    cleaned, _stripped = _strip_invalid_citations(result.content, allowed)
    return cleaned


def _member_chunks_stmt(library_id: uuid.UUID):
    """库内可检索片段（membership 口径与检索一致：回收站/候选不算）。"""
    return (
        select(PaperChunk)
        .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
        .where(
            LibraryPaper.library_id == library_id,
            LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
        )
    )


async def answer(
    session: AsyncSession,
    library_id: uuid.UUID,
    question: str,
    *,
    user_id: uuid.UUID | None,
    max_rounds: int = 2,
) -> dict:
    """库级 agentic RAG 入口：问题 → {answer, evidence, queries}。

    ``evidence`` 每条：{paper_id, chunk_id, title, snippet, score, via}，
    ``via`` ∈ vector（首轮检索）/ expansion（扩展查询）/ citation（引文图补召回）/
    direct（小库直通，全部片段直供）。``queries`` 是实际执行过的检索查询
    （直通分支为空表）。调用方负责库可见性校验。
    """
    from app.core.llm.router import get_llm_router

    llm = get_llm_router()
    library = await session.get(DirectionLibrary, library_id)
    project_id = library.project_id if library is not None else None

    # ---- 小库直通：装得下就全喂，不挑（挑就有漏） ----
    total_chars = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(func.length(PaperChunk.text)), 0))
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
            )
        ).scalar_one()
        or 0
    )
    if total_chars == 0:
        # 没有任何可检索片段：不调 LLM，明说没内容（空证据下作答只会诱导编造）
        return {
            "answer": "文献库中还没有可检索的内容（请先收录论文并建立全文索引）。",
            "evidence": [],
            "queries": [],
        }
    if total_chars // 4 < DIRECT_TOKEN_BUDGET:
        chunks = (
            (
                await session.execute(
                    _member_chunks_stmt(library_id).order_by(
                        PaperChunk.paper_id, PaperChunk.seq
                    )
                )
            )
            .scalars()
            .all()
        )
        direct = [
            _Candidate(chunk=c, score=0.0, via="direct", order=i) for i, c in enumerate(chunks)
        ]
        papers = await _paper_rows(session, list({c.chunk.paper_id for c in direct}))
        evidence = [_evidence_entry(c, papers, score=None) for c in direct]
        text = await _answer_from_evidence(
            llm,
            question=question,
            evidence=evidence,
            summaries_by_chunk={},
            user_id=user_id,
            project_id=project_id,
        )
        return {"answer": text, "evidence": evidence, "queries": []}

    # ---- 1) 迭代查询扩展 ----
    candidates: dict[uuid.UUID, _Candidate] = {}
    executed: list[str] = [question]
    _merge_hits(
        candidates,
        await _search_round(
            session, library_id=library_id, query=question, user_id=user_id, project_id=project_id
        ),
        via="vector",
    )
    for _round in range(max(0, max_rounds - 1)):
        if not candidates:
            break  # 一条都没命中：没有可供扩展的依据，扩展只会放大噪声
        papers = await _paper_rows(
            session, list({c.chunk.paper_id for c in candidates.values()})
        )
        new_queries = await _expand_queries(
            llm,
            question=question,
            executed=executed,
            candidates=candidates,
            papers=papers,
            user_id=user_id,
            project_id=project_id,
        )
        if not new_queries:
            break
        for query in new_queries:
            executed.append(query)
            _merge_hits(
                candidates,
                await _search_round(
                    session,
                    library_id=library_id,
                    query=query,
                    user_id=user_id,
                    project_id=project_id,
                ),
                via="expansion",
            )

    if not candidates:
        return {
            "answer": "文献库中未检索到与问题相关的内容。",
            "evidence": [],
            "queries": executed,
        }

    # ---- 3) 引文图补召回（确定性，无 LLM） ----
    await _citation_supplement(
        session, library_id=library_id, question=question, candidates=candidates
    )

    # ---- 2) 重排 + 摘要 ----
    ordered = sorted(candidates.values(), key=lambda c: (-c.score, c.order))[:RERANK_POOL]
    reranked, rerank_scores, summaries = await _rerank(
        llm, question=question, ordered=ordered, user_id=user_id, project_id=project_id
    )
    top = reranked[:TOP_N_EVIDENCE]
    papers = await _paper_rows(session, list({c.chunk.paper_id for c in top}))
    evidence = [
        _evidence_entry(c, papers, score=rerank_scores.get(c.chunk.id, c.score)) for c in top
    ]
    summaries_by_chunk = {str(cid): text for cid, text in summaries.items()}

    # ---- 4) 证据先行作答 ----
    text = await _answer_from_evidence(
        llm,
        question=question,
        evidence=evidence,
        summaries_by_chunk=summaries_by_chunk,
        user_id=user_id,
        project_id=project_id,
    )
    return {"answer": text, "evidence": evidence, "queries": executed}
