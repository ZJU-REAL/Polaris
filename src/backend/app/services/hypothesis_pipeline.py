"""文献 → 假设四段管线（#648，设计报告 §12，P2 D3；不 import fastapi）。

生成（generate）→ 文献接地（ground）→ 查新（novelty）→ 可行性（feasibility），
供 discovery 引擎（actions_discovery.hypothesis_expand）逐候选调用。全程延续
「确定性 vs 判断性分离」：检索、去重、越界校验、资源信号统计全是确定性代码，
LLM 只负责五件判断性的事——组合灵感成假设、拆子命题、从检索结果里选证据表
立场、判已知/新颖、写风险论证段。

1. **generate**：三路灵感确定性取材——语义近邻（direction 直接检索）、引文图
   远端（命中论文沿引文边走 2 跳、只取非直接邻居：图距离远的实体更可能带来
   跨域组合，MOOSE-Chem「假设 ≈ 背景 + 灵感组合」）、多样窗（未被覆盖论文的
   等距片段窗口，替代随机采样以保证可重放）→ ``hyp_generate`` 一次调用组合出
   n 个候选 → SequenceMatcher 相似度 >0.85 折叠去重（Stanford 100+ 研究者盲评：
   LLM 假设多样性坍缩严重，大量采样后去重是必要步骤，§12）。
2. **ground**：``hyp_ground`` 拆子命题（≤5）→ 逐子命题确定性检索 top-5 →
   同环节第二次调用只允许从检索结果里**选择** paper_id + 立场（support/refute）
   或判 speculation；后处理校验所选 id ∈ 检索集，越界一律降为 speculation——
   引用只能指向真实检索结果，结构上杜绝编造引文（各模型引文伪造率 14–95%）。
3. **novelty**：非 speculation 子命题换措辞再检索一轮（同一措辞查两遍只会
   复读接地结果），``hyp_novelty`` 逐条判 known / novel / uncertain——先摘出
   假设中已知的部分再逐项对照文献，co-scientist Reflection 路线。
4. **feasibility**：venue/年份分布、库内相关片段密度等可得性信号**确定性**
   统计，``hyp_feasibility`` 只写风险论证段——资源匹配是查表不是发挥。

score = novel 比例 × support 覆盖率。选这个最笨的公式是有意的：两个因子各自
可解释、各自单调（新颖子命题越多越值得做，有文献支持的子命题越多越扎实），
乘积天然惩罚偏科——全新颖但无接地是空中楼阁，全接地但不新颖是重复劳动。
加权、校准、两两对比这些精细化留给 D4 锦标赛，先让分数「能解释给人听」。
"""

import json
import uuid
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperChunk
from app.models.paper_citation import PaperCitation
from app.services import chunks as chunks_service
from app.services import library_rag
from app.services.papers import PAPER_STATUS_GROUPS

# 四个 LLM 环节（router.py STAGES / 前端 LLM_STAGES 同步登记）：
# generate/ground 是较长的结构化生成走中档，novelty/feasibility 是短 JSON 判定走短档
GENERATE_STAGE = "hyp_generate"
GROUND_STAGE = "hyp_ground"
NOVELTY_STAGE = "hyp_novelty"
FEASIBILITY_STAGE = "hyp_feasibility"

DEFAULT_CANDIDATES = 6  # generate 默认一次要多少候选（去重前）
MAX_SUBCLAIMS = 5  # 子命题上限：多了检索与判定成本线性涨，而假设该是聚焦的
GROUND_TOP_K = 5  # 每条子命题送给 LLM 挑选的检索结果数
DEDUP_THRESHOLD = 0.85  # 陈述相似度超过它视为同一假设（去重折叠）
INSPIRATION_PER_ROUTE = 3  # 引文远端 / 多样窗两路各取几条灵感
SNIPPET_CHARS = 300  # 灵感与证据片段的截断长度
DENSITY_LIMIT = 50  # 相关片段密度统计的计数上限（信号要的是量级，不是全量）
# expand 后低于此分自动剪枝（actions_discovery 消费）。0.15 = 「不到一半子命题
# 新颖 × 不到一半有支持」量级以下：两个维度都不及格的假设不值得占用后续预算
MIN_VIABLE_SCORE = 0.15

_MAX_JSON_ATTEMPTS = 3

GENERATE_SYSTEM_PROMPT = """\
POLARIS_HYP_GENERATE
你是研究假设生成器。输入 JSON 里是研究方向和三路文献灵感（semantic=语义近邻、
citation_far=引文图远端、diverse=多样窗口片段）。请把方向与灵感**组合**成最多 n 个
彼此不同、可检验的研究假设（鼓励跨片段组合，而不是逐条改写）。
只输出 JSON：{"candidates": [{"statement": "假设陈述（一句话，可检验）", \
"rationale": "一句话依据（引用了哪路灵感）"}]}"""

GROUND_SPLIT_SYSTEM_PROMPT = """\
POLARIS_HYP_GROUND
你是假设分解器。把输入 JSON 里的假设陈述拆成最多 5 条可独立检索验证的子命题
（每条一句话，合起来覆盖假设的关键断言）。
只输出 JSON：{"subclaims": ["...", "..."]}"""

GROUND_SELECT_SYSTEM_PROMPT = """\
POLARIS_HYP_GROUND
你是文献接地判定器。输入 JSON 的 items 每条是一个子命题和为它检索到的文献片段
（各带 paper_id）。对每条子命题：若某些片段支持/反驳它，给出立场和所依据的
paper_id 列表；检索结果都不相关时判 speculation（推测，paper_ids 留空）。
**只允许使用 retrieved 里出现过的 paper_id，禁止编造。**
只输出 JSON：{"items": [{"index": <原样回传>, \
"stance": "support|refute|speculation", "paper_ids": ["..."]}]}"""

NOVELTY_SYSTEM_PROMPT = """\
POLARIS_HYP_NOVELTY
你是查新判定器。输入 JSON 的 items 每条是假设的一个子命题和再次检索到的文献
片段。对每条判定：该子命题是否已被这些文献覆盖——
known（已有文献明确覆盖）| novel（文献未覆盖，是新的）| uncertain（证据不足以判定）。
paper_ids 只允许来自 evidence。
只输出 JSON：{"items": [{"index": <原样回传>, \
"verdict": "known|novel|uncertain", "paper_ids": ["..."]}]}"""

FEASIBILITY_SYSTEM_PROMPT = """\
POLARIS_HYP_FEASIBILITY
你是可行性风险论证员。输入 JSON 里是假设陈述和已经确定性统计好的资源信号
（接地文献数、venue/年份分布、库内相关片段密度）。**不要重新估计这些数字**，
只基于它们写一段简短的风险论证（哪些信号偏弱、验证该假设前要补什么）。
只输出 JSON：{"risk_note": "..."}"""


# ---- 通用小件 ----


def _merge_usage(total: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    total["prompt_tokens"] += int((usage or {}).get("prompt_tokens", 0) or 0)
    total["completion_tokens"] += int((usage or {}).get("completion_tokens", 0) or 0)
    return total


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(
    llm: LLMRouter,
    stage: str,
    *,
    system: str,
    payload: dict[str, Any],
    validate,
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
) -> Any:
    """LLM JSON 请求：解析/校验失败重试，重试轮次的 token 也如实并入 usage。"""
    last_error: Exception | None = None
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        result = await llm.complete(
            stage,
            [
                Message(role="system", content=system),
                Message(role="user", content=json.dumps(payload, ensure_ascii=False)),
            ],
            temperature=0.0,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
        _merge_usage(usage, result.usage)
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"{stage} 连续输出非法 JSON：{last_error}")


def _ordered_paper_ids(chunks: list[PaperChunk]) -> list[str]:
    """片段列表 → 去重保序的 paper_id 字符串列表（检索留痕用，D6 披露 #655）。"""
    out: list[str] = []
    for chunk in chunks:
        pid = str(chunk.paper_id)
        if pid not in out:
            out.append(pid)
    return out


async def _paper_titles(
    session: AsyncSession, paper_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not paper_ids:
        return {}
    rows = (
        await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(paper_ids)))
    ).all()
    return {pid: (title or "") for pid, title in rows}


def _chunk_entry(chunk: PaperChunk, titles: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "paper_id": str(chunk.paper_id),
        "title": titles.get(chunk.paper_id, ""),
        "snippet": chunk.text[:SNIPPET_CHARS],
    }


async def _member_paper_ids(session: AsyncSession, library_id: uuid.UUID) -> set[uuid.UUID]:
    """库内成员论文集合（回收站/候选不算）：所有灵感与证据都不许越过库边界。"""
    return set(
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


async def _retrieve(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    query: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[PaperChunk]:
    """确定性检索原语：复用 RAG 的单轮检索（向量优先、关键词降级），截前 limit 条。

    刻意不走 library_rag.answer 的完整流水线——那里面有查询扩展和重排两次 LLM
    判断，接地/查新要的是「同样输入永远同样证据集」的确定性检索。
    """
    rows = await library_rag.retrieve_round(
        session, library_id=library_id, query=query, user_id=user_id, project_id=project_id
    )
    return [chunk for chunk, _score in rows[:limit]]


# ---- 1) 生成：三路灵感 → 一次组合 → 去重 ----


async def _citation_far_chunks(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    seed_paper_ids: list[uuid.UUID],
) -> list[PaperChunk]:
    """引文图远端灵感：从命中论文沿引文边走 2 跳，只取**非直接邻居**的库内论文。

    直接邻居（引用/被引）与种子的相关性语义检索多半也够得着；隔一层的论文才是
    「结构上有关联、语义上够得远」的跨域灵感来源。逐跳 BFS 而非递归 CTE：假设树
    同款理由——两跳的小查询，可读性优先。
    """
    if not seed_paper_ids:
        return []
    member_ids = await _member_paper_ids(session, library_id)

    async def neighbors(frontier: list[uuid.UUID]) -> list[uuid.UUID]:
        edges = (
            (
                await session.execute(
                    select(PaperCitation)
                    .where(
                        PaperCitation.citing_paper_id.in_(frontier)
                        | PaperCitation.cited_paper_id.in_(frontier)
                    )
                    .order_by(PaperCitation.citing_paper_id, PaperCitation.ref_index)
                )
            )
            .scalars()
            .all()
        )
        out: list[uuid.UUID] = []
        for edge in edges:
            for pid in (edge.cited_paper_id, edge.citing_paper_id):
                if pid is not None and pid not in frontier and pid not in out:
                    out.append(pid)
        return out

    seeds = list(seed_paper_ids)
    hop1 = [pid for pid in await neighbors(seeds) if pid not in seeds]
    if not hop1:
        return []
    hop2 = [
        pid
        for pid in await neighbors(hop1)
        if pid not in seeds and pid not in hop1 and pid in member_ids
    ][:INSPIRATION_PER_ROUTE]
    if not hop2:
        return []
    # 每篇远端论文取开头片段：远端灵感靠「它讲什么」而非「哪句最像方向」入选
    chunks = (
        (
            await session.execute(
                select(PaperChunk)
                .where(PaperChunk.paper_id.in_(hop2))
                .order_by(PaperChunk.paper_id, PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    first_by_paper: dict[uuid.UUID, PaperChunk] = {}
    for chunk in chunks:
        first_by_paper.setdefault(chunk.paper_id, chunk)
    return [first_by_paper[pid] for pid in hop2 if pid in first_by_paper]


async def _diverse_window_chunks(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    exclude_paper_ids: set[uuid.UUID],
) -> list[PaperChunk]:
    """多样窗灵感：未被前两路覆盖的库内片段，按等距窗口取样。

    「随机多样窗」的随机在这里被有意替换成等距取样：discovery run 的每一步都要
    可重放（断点恢复 / golden 比对），随机源会让同一 run 两次重放长出不同的树。
    等距窗口保留了「打散位置、覆盖不同论文段落」的多样性收益，又是纯确定性的。
    """
    rows = (
        (
            await session.execute(
                select(PaperChunk)
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
                .order_by(PaperChunk.paper_id, PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    pool = [c for c in rows if c.paper_id not in exclude_paper_ids] or list(rows)
    if not pool:
        return []
    stride = max(1, len(pool) // INSPIRATION_PER_ROUTE)
    return pool[::stride][:INSPIRATION_PER_ROUTE]


def _norm_statement(text: str) -> str:
    return " ".join(str(text).lower().split())


def dedup_candidates(
    candidates: list[dict[str, Any]], *, threshold: float = DEDUP_THRESHOLD
) -> list[dict[str, Any]]:
    """按陈述相似度折叠近重复候选（保留先出现的——LLM 输出顺序即其置信排序）。

    SequenceMatcher 而非嵌入相似度：去重要拦的是「同一句话换个说法」这种表层
    重复（多样性坍缩的典型形态），字符级相似度便宜、确定、够用。
    """
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        norm = _norm_statement(cand.get("statement") or "")
        if not norm:
            continue
        duplicate = any(
            SequenceMatcher(None, norm, _norm_statement(k["statement"])).ratio() > threshold
            for k in kept
        )
        if not duplicate:
            kept.append(cand)
    return kept


def _validate_generate(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ValueError('generate 输出需含 "candidates" 列表')
    out = []
    for raw in data["candidates"]:
        if not isinstance(raw, dict) or not str(raw.get("statement") or "").strip():
            continue
        out.append(
            {
                "statement": str(raw["statement"]).strip(),
                "rationale": str(raw.get("rationale") or "").strip(),
            }
        )
    if not out:
        raise ValueError("generate 未给出任何含非空 statement 的候选")
    return out


async def generate(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    direction: str,
    n_candidates: int = DEFAULT_CANDIDATES,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """三路灵感 → 一次 LLM 组合出 n 个候选 → 去重。

    返回 {"candidates": [{statement, rationale}], "usage": {...}, "trace": {...}}；
    candidates 已去重，数量 ≤ n_candidates（可能因折叠更少）。trace 是确定性
    检索留痕（查询 + 捞回的 paper_ids），供 expand 记进轮次账本、D6 披露消费。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    semantic_chunks = await _retrieve(
        session,
        library_id=library_id,
        query=direction,
        user_id=user_id,
        project_id=project_id,
        limit=library_rag.TOP_K,
    )
    covered = {c.paper_id for c in semantic_chunks}
    seed_papers: list[uuid.UUID] = []
    for chunk in semantic_chunks:
        if chunk.paper_id not in seed_papers:
            seed_papers.append(chunk.paper_id)
    far_chunks = await _citation_far_chunks(
        session, library_id=library_id, seed_paper_ids=seed_papers[:INSPIRATION_PER_ROUTE]
    )
    covered |= {c.paper_id for c in far_chunks}
    diverse_chunks = await _diverse_window_chunks(
        session, library_id=library_id, exclude_paper_ids=covered
    )
    all_ids = list({c.paper_id for c in semantic_chunks + far_chunks + diverse_chunks})
    titles = await _paper_titles(session, all_ids)
    payload = {
        "direction": direction,
        "n": n_candidates,
        "inspirations": {
            "semantic": [_chunk_entry(c, titles) for c in semantic_chunks],
            "citation_far": [_chunk_entry(c, titles) for c in far_chunks],
            "diverse": [_chunk_entry(c, titles) for c in diverse_chunks],
        },
    }
    raw = await _complete_json(
        llm,
        GENERATE_STAGE,
        system=GENERATE_SYSTEM_PROMPT,
        payload=payload,
        validate=_validate_generate,
        usage=usage,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    return {
        "candidates": dedup_candidates(raw[:n_candidates]),
        "usage": usage,
        # 检索留痕（确定性数据，D6 披露 #655）：发出过什么查询、捞回了哪些论文。
        # 三路灵感只有语义路有查询文本；引文远端/多样窗按路记论文集合。
        "trace": {
            "queries": [
                {"query": direction, "paper_ids": _ordered_paper_ids(semantic_chunks)}
            ],
            "inspiration_paper_ids": {
                "semantic": _ordered_paper_ids(semantic_chunks),
                "citation_far": _ordered_paper_ids(far_chunks),
                "diverse": _ordered_paper_ids(diverse_chunks),
            },
        },
    }


# ---- 2) 文献接地：拆子命题 → 确定性检索 → 只许从检索集内选 ----


def _validate_subclaims(data: Any) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("subclaims"), list):
        raise ValueError('ground 拆分输出需含 "subclaims" 列表')
    out = [str(s).strip() for s in data["subclaims"] if str(s or "").strip()]
    if not out:
        raise ValueError("ground 未拆出任何非空子命题")
    return out[:MAX_SUBCLAIMS]


def _validate_indexed_items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError('输出需含 "items" 列表')
    return [it for it in data["items"] if isinstance(it, dict)]


def sanitize_grounding(
    subclaims: list[str],
    retrieved: list[list[dict[str, Any]]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """接地后处理（纯函数，越界校验的落点）：

    - 立场只认 support/refute，其余（含缺失/乱写）一律 speculation；
    - paper_ids 逐条与**该子命题自己的**检索集求交，越界 id 直接丢弃；
    - 立场是 support/refute 但没剩下任何合法 id → 降为 speculation——
      「有立场没证据」和编造引文一样不可信；
    - snippets 回填所选论文在检索集里的片段，证据卡（D5）直接可用。
    """
    by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        idx = item.get("index")
        if isinstance(idx, int) and idx not in by_index:
            by_index[idx] = item
    out: list[dict[str, Any]] = []
    for i, subclaim in enumerate(subclaims):
        pool = retrieved[i] if i < len(retrieved) else []
        allowed = {entry["paper_id"] for entry in pool}
        raw = by_index.get(i) or {}
        stance = str(raw.get("stance") or "")
        ids = [str(p) for p in (raw.get("paper_ids") or []) if str(p) in allowed]
        # 同一论文多次入选只留一次（顺序保持）
        ids = list(dict.fromkeys(ids))
        if stance not in ("support", "refute") or not ids:
            stance, ids = "speculation", []
        chosen = set(ids)
        snippets = [entry["snippet"] for entry in pool if entry["paper_id"] in chosen]
        out.append(
            {"subclaim": subclaim, "stance": stance, "paper_ids": ids, "snippets": snippets}
        )
    return out


async def ground(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """假设 → grounding JSON（与 models/hypothesis.py 的 D1 注释一致）：

    [{"subclaim", "stance": "support|refute|speculation", "paper_ids", "snippets"}]
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    identity = dict(
        user_id=user_id, project_id=project_id, library_id=library_id, voyage_id=voyage_id
    )
    subclaims = await _complete_json(
        llm,
        GROUND_STAGE,
        system=GROUND_SPLIT_SYSTEM_PROMPT,
        payload={"statement": statement},
        validate=_validate_subclaims,
        usage=usage,
        **identity,
    )
    retrieved: list[list[dict[str, Any]]] = []
    trace_queries: list[dict[str, Any]] = []
    for subclaim in subclaims:
        chunks = await _retrieve(
            session,
            library_id=library_id,
            query=subclaim,
            user_id=user_id,
            project_id=project_id,
            limit=GROUND_TOP_K,
        )
        titles = await _paper_titles(session, list({c.paper_id for c in chunks}))
        retrieved.append([_chunk_entry(c, titles) for c in chunks])
        # 检索留痕（D6 披露 #655）：接地的查询就是子命题本身
        trace_queries.append({"query": subclaim, "paper_ids": _ordered_paper_ids(chunks)})
    items = await _complete_json(
        llm,
        GROUND_STAGE,
        system=GROUND_SELECT_SYSTEM_PROMPT,
        payload={
            "statement": statement,
            "items": [
                {"index": i, "subclaim": sub, "retrieved": retrieved[i]}
                for i, sub in enumerate(subclaims)
            ],
        },
        validate=_validate_indexed_items,
        usage=usage,
        **identity,
    )
    return {
        "grounding": sanitize_grounding(subclaims, retrieved, items),
        "usage": usage,
        "trace": {"queries": trace_queries},
    }


# ---- 3) 查新：换措辞再检索 → 逐子命题 judge ----

# 换措辞的确定性前缀：目的不是「更好的查询」，而是**换一个角度**再捞一轮——
# 同一措辞查两遍只会复读接地结果，查新就成了自证
_NOVELTY_QUERY_PREFIX = "已有研究 prior work on"

_NOVELTY_VERDICTS = ("known", "novel", "uncertain")


def novelty_ratio(report: dict[str, Any]) -> float:
    """novel 子命题占比（score 公式的一半；空报告为 0）。"""
    subclaims = report.get("subclaims") or []
    if not subclaims:
        return 0.0
    novel = sum(1 for e in subclaims if e.get("verdict") == "novel")
    return novel / len(subclaims)


async def novelty(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    grounding: list[dict[str, Any]],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """逐子命题查新 → novelty_report：

    {"subclaims": [{"subclaim", "verdict": "known|novel|uncertain",
                    "paper_ids", "judged"}]}

    speculation 子命题不送 judge（库里连接地证据都没有，「是否已被覆盖」无从
    判起），如实记 uncertain / judged=False。judge 输出解析失败也整体降级为
    uncertain——查新判不动不等于假设不成立，不该炸掉整轮扩展。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    judged_indices = [i for i, e in enumerate(grounding) if e.get("stance") != "speculation"]
    evidence_by_index: dict[int, list[dict[str, Any]]] = {}
    trace_queries: list[dict[str, Any]] = []
    for i in judged_indices:
        query = f"{_NOVELTY_QUERY_PREFIX} {grounding[i]['subclaim']}"
        chunks = await _retrieve(
            session,
            library_id=library_id,
            query=query,
            user_id=user_id,
            project_id=project_id,
            limit=GROUND_TOP_K,
        )
        titles = await _paper_titles(session, list({c.paper_id for c in chunks}))
        evidence_by_index[i] = [_chunk_entry(c, titles) for c in chunks]
        # 检索留痕（D6 披露 #655）：查新用的是换措辞后的查询，如实记换后的
        trace_queries.append({"query": query, "paper_ids": _ordered_paper_ids(chunks)})
    verdicts: dict[int, tuple[str, list[str]]] = {}
    if judged_indices:
        try:
            items = await _complete_json(
                llm,
                NOVELTY_STAGE,
                system=NOVELTY_SYSTEM_PROMPT,
                payload={
                    "statement": statement,
                    "items": [
                        {
                            "index": i,
                            "subclaim": grounding[i]["subclaim"],
                            "evidence": evidence_by_index[i],
                        }
                        for i in judged_indices
                    ],
                },
                validate=_validate_indexed_items,
                usage=usage,
                user_id=user_id,
                project_id=project_id,
                library_id=library_id,
                voyage_id=voyage_id,
            )
            for item in items:
                idx = item.get("index")
                if not isinstance(idx, int) or idx not in evidence_by_index:
                    continue
                verdict = str(item.get("verdict") or "")
                if verdict not in _NOVELTY_VERDICTS:
                    verdict = "uncertain"
                allowed = {e["paper_id"] for e in evidence_by_index[idx]}
                ids = [str(p) for p in (item.get("paper_ids") or []) if str(p) in allowed]
                verdicts[idx] = (verdict, list(dict.fromkeys(ids)))
        except ValueError:
            verdicts = {}  # 判不动 → 全部 uncertain（诚实缺省）
    subclaim_reports = []
    for i, entry in enumerate(grounding):
        verdict, ids = verdicts.get(i, ("uncertain", []))
        subclaim_reports.append(
            {
                "subclaim": entry["subclaim"],
                "verdict": verdict,
                "paper_ids": ids,
                "judged": i in verdicts,
            }
        )
    return {
        "report": {"subclaims": subclaim_reports},
        "usage": usage,
        "trace": {"queries": trace_queries},
    }


# ---- 4) 可行性：确定性资源信号 + LLM 只写风险论证 ----


def _validate_risk_note(data: Any) -> str:
    if not isinstance(data, dict) or not str(data.get("risk_note") or "").strip():
        raise ValueError('feasibility 输出需含非空 "risk_note"')
    return str(data["risk_note"]).strip()


async def feasibility(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    grounding: list[dict[str, Any]],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """feasibility JSON = {"signals": {确定性信号}, "risk_note": LLM 风险论证}。

    signals 全部由代码统计（§12：资源匹配是确定性查表）：
    - grounded_paper_count / venues / year_range：接地文献的规模与来源分布，
      「有多少扎实文献托底、来自哪里、新不新」；
    - related_chunk_hits：库内与该假设相关的片段密度（关键词口径），密度低
      意味着验证材料在库内很薄，得先扩充语料。
    """
    grounded_ids: list[uuid.UUID] = []
    for entry in grounding:
        if entry.get("stance") == "speculation":
            continue
        for raw in entry.get("paper_ids") or []:
            pid = uuid.UUID(str(raw))
            if pid not in grounded_ids:
                grounded_ids.append(pid)
    papers = []
    if grounded_ids:
        papers = (
            (await session.execute(select(Paper).where(Paper.id.in_(grounded_ids))))
            .scalars()
            .all()
        )
    venues: dict[str, int] = {}
    years: list[int] = []
    for paper in papers:
        if paper.venue:
            venues[paper.venue] = venues.get(paper.venue, 0) + 1
        if paper.year:
            years.append(int(paper.year))
    try:
        related_hits = len(
            await chunks_service.keyword_search_chunks(
                session, library_ids=[library_id], q=statement, limit=DENSITY_LIMIT
            )
        )
    except Exception:  # noqa: BLE001 — 密度只是参考信号，统计失败按 0 记不上抛
        related_hits = 0
    signals = {
        "grounded_paper_count": len(papers),
        "venues": dict(sorted(venues.items())),
        "year_range": [min(years), max(years)] if years else None,
        "related_chunk_hits": related_hits,
    }
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        risk_note = await _complete_json(
            llm,
            FEASIBILITY_STAGE,
            system=FEASIBILITY_SYSTEM_PROMPT,
            payload={"statement": statement, "signals": signals},
            validate=_validate_risk_note,
            usage=usage,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    except ValueError:
        # 风险论证写不出来不影响信号本身的价值：信号照常入库，note 如实说不可用
        risk_note = "（风险论证生成失败，仅供确定性信号参考。）"
    return {"feasibility": {"signals": signals, "risk_note": risk_note}, "usage": usage}


# ---- 评分 ----


def score_hypothesis(
    grounding: list[dict[str, Any]], novelty_report: dict[str, Any]
) -> float:
    """score = novel 比例 × support 覆盖率（见模块 docstring 的 why）。"""
    total = len(grounding)
    if total == 0:
        return 0.0
    support = sum(1 for e in grounding if e.get("stance") == "support")
    return round(novelty_ratio(novelty_report) * (support / total), 4)
