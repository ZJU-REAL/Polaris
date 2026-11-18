"""调研检索：为「一个研究方向的文献调研」找候选论文（不碰数据库）。

和建库的日常检索不是一回事。日常检索问的是「最近有什么」——把全部关键词 OR 在
一起、按提交日期倒序取前 N 篇。拿它做调研，实测 30 篇候选里 24 篇不相干：只要摘要里
出现了「retrieval-augmented generation」这几个词的最新论文都会进来，而真正回答这个
问题的经典工作一篇都没有。调研要的是「和这个问题最相关的是哪些」：

1. **每条查询单独查，按相关度排**。查询由模型从简报写出来（具体短语、方法名、数据集
   名、它知道的代表性论文标题）——只当检索词用，论文记录一律来自来源的真实返回；
2. **不按回溯月数硬截**：相关度这一遍放到近十年，奠基工作才进得来；回溯月数只用来
   保证结果里有一部分是近期的（约三成）；
3. 各来源同时查、各有总时限，一个来源慢或被限流只是它这次缺席；
4. 跨来源按 DOI / arXiv 编号 / 规范化标题合并，按「在多少条查询、多少个来源里排在
   前面」融合排序（倒数排名之和，略偏向高被引），再取前 N 篇。

来源失败、返回得少，它的份额自然由别的来源补上——没有按来源预分的名额。
每一次请求（来源、查询、排序、时间窗、返回条数、错误）都记下来，随步骤结果给人看。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.core.llm.base import Message

logger = logging.getLogger(__name__)

QUERY_STAGE = "research_brief"
#: 相关度这一遍往回看多少年（奠基工作要进得来）
FOUNDATION_YEARS = 10
#: 结果里留给「近期工作」（回溯月数以内）的份额
RECENT_SHARE = 0.3
#: 每个来源这次检索总共最多花多久；到点就用已经拿到的
SOURCE_TIME_BUDGET_S = 90.0
MIN_QUERIES, MAX_QUERIES = 4, 12
_RRF_K = 10.0
_CITATION_WEIGHT = 0.015

QUERY_SYSTEM_PROMPT = """\
POLARIS_SURVEY_QUERIES
你在为一次文献调研写检索词。给你一份研究简报，写 8–12 条**英文**检索查询，用来在
arXiv、OpenAlex、Crossref 这类学术搜索里按相关度找论文。要求：
- 每条 2–8 个词，是论文标题或摘要里会出现的说法，不要写成问句，不要加引号和布尔运算符；
- 前几条覆盖问题的核心（最具体的短语在前）；
- 包含这个问题上你知道的代表性方法名、模型名、数据集或基准名；
- 包含 2–4 条你知道的奠基性或代表性论文的标题（或标题里最有辨识度的那一段）；
  它们只当检索词用，记不准就别写；
- 不要宽泛到只剩领域名（例如只写 "machine learning"）；
- 条目之间不要只差一两个虚词。
"""


class SearchQueries(BaseModel):
    """模型写出来的检索查询（结构化输出）。"""

    queries: list[str]


@dataclass
class QueryLog:
    source: str
    query: str
    sort: str
    window: str
    returned: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "query": self.query,
            "sort": self.sort,
            "window": self.window,
            "returned": self.returned,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Candidate:
    entry: dict[str, Any]
    sources: set[str] = field(default_factory=set)
    score: float = 0.0
    citations: int = 0
    recent: bool = False


@dataclass
class SurveyResult:
    selected: list[Candidate]
    logs: list[QueryLog]
    #: 每个来源返回了多少条（去重前）
    fetched: dict[str, int]
    #: 最后选中的候选里，每个来源贡献了多少篇（一篇被多个来源找到时各算一次）
    selected_by_source: dict[str, int]
    #: 失败的来源 [{source, error}]；拿到一部分之后才失败的也在里面
    errors: list[dict[str, str]]
    unique: int = 0  # 去重后一共有多少篇候选


# ---- 查询 ----


def _clean_query(raw: Any) -> str:
    text = re.sub(r'["“”‘’()\[\]{}:]', " ", str(raw or ""))
    text = re.sub(r"\b(AND|OR|NOT|ANDNOT)\b", " ", text)
    return " ".join(text.split())[:160]


def merge_queries(*groups: list[str], limit: int = MAX_QUERIES) -> list[str]:
    """几组查询合成一份：去掉空的、含中文的（来源按英文检索）和重复的，保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for raw in group or []:
            query = _clean_query(raw)
            key = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
            if not key or key in seen or re.search(r"[㐀-鿿]", query):
                continue
            seen.add(key)
            out.append(query)
    return out[:limit]


async def plan_queries(
    llm: Any,
    *,
    statement: str,
    questions: list[str],
    keywords: list[str],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[list[str], str | None]:
    """从简报写出检索查询，返回 (查询, 没用上模型时的原因)。

    模型写的排前面，简报里的关键词补在后面。模型这一步失败不让检索失败：退回只用
    关键词——少了方法名和代表作标题，结果会差一些，所以把原因带出去记进诊断。
    """
    brief = "\n".join(
        part
        for part in (
            f"研究方向：{statement.strip()}" if statement.strip() else "",
            "研究问题：" + "；".join(questions) if questions else "",
            "已有的英文关键词：" + "; ".join(keywords) if keywords else "",
        )
        if part
    )
    try:
        result = await llm.complete_json(
            QUERY_STAGE,
            [
                Message(role="system", content=QUERY_SYSTEM_PROMPT),
                Message(role="user", content=brief or "（没有简报）"),
            ],
            schema=SearchQueries,
            max_tokens=4000,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — 见上：退回只用关键词
        logger.warning("survey query planning failed; using the brief keywords", exc_info=True)
        return merge_queries(keywords), f"{type(e).__name__}"
    planned = merge_queries(list(result.data.queries), keywords)
    if len(planned) < MIN_QUERIES:
        planned = merge_queries(planned, keywords)
    return planned, None


_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is",
        "it", "its", "of", "on", "or", "over", "the", "to", "under", "via", "with", "without",
        "using", "use", "based", "towards", "toward",
    }
)  # fmt: skip


def arxiv_expression(query: str) -> str:
    """一条查询 → arXiv 检索式。

    短的（四个词以内）当短语在标题和摘要里找；长的（多半是论文标题或一句描述）
    整句当短语几乎搜不到东西，改成要求每个实词都出现。
    """
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\+\.]*", query) if w]
    if not words:
        return 'all:""'
    if len(words) <= 4:
        phrase = " ".join(words)
        return f'ti:"{phrase}" OR abs:"{phrase}"'
    content = [w for w in words if w.lower() not in _STOPWORDS][:7] or words[:7]
    return " AND ".join(f"all:{w}" for w in content)


# ---- 各来源 ----


def _iso_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def entry_from_openalex(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "arxiv_id": row.get("arxiv_id"),
        "title": row.get("title"),
        "doi": row.get("doi"),
        "pmid": row.get("pmid"),
        "authors": row.get("authors") or [],
        "abstract": row.get("abstract"),
        "year": row.get("year"),
        "primary_category": row.get("venue"),
        "url": row.get("url"),
        "published": _iso_or_none(row.get("published")),
        "citations": row.get("cited_by_count") or 0,
    }


def entry_from_candidate(item: Any) -> dict[str, Any]:
    """通用来源的 LiteratureCandidate → 入库用的条目。"""
    meta = item.metadata if isinstance(getattr(item, "metadata", None), dict) else {}
    return {
        "arxiv_id": getattr(item, "arxiv_id", None) or meta.get("arxiv_id") or meta.get("arxiv"),
        "title": item.title,
        "doi": item.doi,
        "pmid": getattr(item, "pmid", None),
        "authors": item.authors,
        "abstract": item.abstract,
        "year": item.year,
        "primary_category": item.venue,
        "url": item.url,
        "published": _iso_or_none(meta.get("published")),
        "citations": getattr(item, "citation_count", None) or 0,
    }


class _Collector:
    """各来源把每一条查询的结果交到这里（按返回顺序 = 来源给的相关度顺序）。"""

    def __init__(self) -> None:
        self.lists: list[tuple[str, list[dict[str, Any]]]] = []
        self.logs: list[QueryLog] = []
        self.fetched: dict[str, int] = {}
        self.errors: dict[str, str] = {}

    def add(self, log: QueryLog, entries: list[dict[str, Any]]) -> None:
        log.returned = len(entries)
        self.logs.append(log)
        self.fetched[log.source] = self.fetched.get(log.source, 0) + len(entries)
        if entries:
            self.lists.append((log.source, entries))

    def fail(self, log: QueryLog, error: BaseException | str) -> None:
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}".strip(": ")
        log.error = text[:300]
        self.logs.append(log)
        self.errors.setdefault(log.source, log.error)


def _window(since: datetime | None, until: datetime) -> str:
    return f"{since.date().isoformat() if since else '…'}..{until.date().isoformat()}"


async def _search_arxiv(
    adapter: Any,
    queries: list[str],
    *,
    foundation_since: datetime,
    recent_since: datetime,
    now: datetime,
    per_query: int,
    out: _Collector,
) -> None:
    search = getattr(adapter, "search_relevance", None)
    if search is None or not hasattr(getattr(adapter, "client", None), "search_relevance"):
        # 注入的替身没有这个方法：走通用检索
        await _search_generic(
            adapter, "arxiv", queries, foundation_since=foundation_since, now=now,
            per_query=per_query, out=out,
        )  # fmt: skip
        return
    # 近期这一遍只发一次请求：前几条查询 OR 在一起、限定在回溯窗口里。arXiv 要求
    # 请求间隔 3 秒，每条查询各发一遍近期的会把这个来源的时间翻倍
    recent_expr = " OR ".join(f"({arxiv_expression(q)})" for q in queries[:4])
    plan = [(q, arxiv_expression(q), foundation_since, per_query) for q in queries]
    plan.append(("recent: " + " | ".join(queries[:4]), recent_expr, recent_since, per_query * 2))
    for label, expression, since, limit in plan:
        log = QueryLog("arxiv", expression, "relevance", _window(since, now))
        try:
            rows = await search(expression, since=since, until=now, limit=limit, max_attempts=2)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 限流最常见：这个来源到此为止，已拿到的照用
            logger.warning("arXiv survey query failed (%s); skipping the rest", label)
            out.fail(log, e)
            return
        out.add(log, [dict(r) | {"citations": 0} for r in rows if isinstance(r, dict)])


async def _search_openalex(
    adapter: Any,
    queries: list[str],
    *,
    foundation_since: datetime,
    recent_since: datetime,
    now: datetime,
    per_query: int,
    out: _Collector,
) -> None:
    search = getattr(adapter, "search_works", None)
    if search is None or not hasattr(getattr(adapter, "client", None), "search_works"):
        await _search_generic(
            adapter, "openalex", queries, foundation_since=foundation_since, now=now,
            per_query=per_query, out=out,
        )  # fmt: skip
        return
    plan = [(q, foundation_since) for q in queries] + [(q, recent_since) for q in queries[:4]]
    failures = 0
    for query, since in plan:
        log = QueryLog("openalex", query, "relevance", _window(since, now))
        try:
            rows = await search(query, limit=per_query, start_year=since.year, end_year=now.year)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单条查询失败不拖累其余；连着失败就停
            out.fail(log, e)
            failures += 1
            if failures >= 2:
                return
            continue
        out.add(log, [entry_from_openalex(r) for r in rows if isinstance(r, dict)])


async def _search_generic(
    adapter: Any,
    source_id: str,
    queries: list[str],
    *,
    foundation_since: datetime,
    now: datetime,
    per_query: int,
    out: _Collector,
) -> None:
    from app.schemas.literature_discovery import SourceSearchRequest

    failures = 0
    for query in queries:
        log = QueryLog(source_id, query, "relevance", f"{foundation_since.year}..{now.year}")
        try:
            page = await adapter.search(
                SourceSearchRequest(
                    query=query,
                    start_year=foundation_since.year,
                    end_year=now.year,
                    limit=per_query,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            out.fail(log, e)
            failures += 1
            if failures >= 2:  # 连着两条都失败：这个来源这次不行，不再一条条撞
                return
            continue
        out.add(log, [entry_from_candidate(item) for item in page.items])


# ---- 合并、排序、取前 N ----


def _norm_title(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _keys(entry: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    doi = str(entry.get("doi") or "").strip().lower()
    if doi:
        keys.append(f"doi:{doi}")
        if doi.startswith("10.48550/arxiv."):
            keys.append(f"arxiv:{doi.removeprefix('10.48550/arxiv.')}")
    arxiv_id = re.sub(r"v\d+$", "", str(entry.get("arxiv_id") or "").strip().lower())
    if arxiv_id:
        keys.append(f"arxiv:{arxiv_id}")
    title = _norm_title(entry.get("title"))
    if len(title) >= 12:
        keys.append(f"title:{title}")
    return keys


def _merge_entry(into: dict[str, Any], other: dict[str, Any]) -> None:
    """同一篇论文的两条记录合成一条：缺的补上，摘要取长的。"""
    for key, value in other.items():
        if key == "abstract":
            if len(str(value or "")) > len(str(into.get("abstract") or "")):
                into["abstract"] = value
        elif key == "citations":
            into["citations"] = max(int(into.get("citations") or 0), int(value or 0))
        elif not into.get(key) and value:
            into[key] = value


def _entry_date(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("published")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    year = entry.get("year")
    if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 3000:
        return datetime(year, 7, 1, tzinfo=UTC)
    return None


def fuse(
    lists: list[tuple[str, list[dict[str, Any]]]],
    *,
    recent_since: datetime,
    exclude: list[str] | None = None,
) -> list[Candidate]:
    """把各来源、各查询的结果合并去重并打融合分，按分从高到低返回。"""
    excluded = [t.strip().lower() for t in (exclude or []) if str(t).strip()]
    by_key: dict[str, Candidate] = {}
    ordered: list[Candidate] = []
    for source, entries in lists:
        seen_here: set[int] = set()
        for rank, entry in enumerate(entries):
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            text = f"{title} {entry.get('abstract') or ''}".lower()
            if any(term in text for term in excluded):
                continue
            keys = _keys(entry)
            found = next((by_key[k] for k in keys if k in by_key), None)
            if found is None:
                found = Candidate(entry=dict(entry))
                ordered.append(found)
            else:
                _merge_entry(found.entry, entry)
            for k in (*keys, *_keys(found.entry)):
                by_key.setdefault(k, found)
            found.sources.add(source)
            if id(found) not in seen_here:  # 同一份结果里重复出现只算一次
                seen_here.add(id(found))
                found.score += 1.0 / (_RRF_K + rank)
    for cand in ordered:
        cand.citations = int(cand.entry.get("citations") or 0)
        cand.score += _CITATION_WEIGHT * math.log10(1 + max(0, cand.citations))
        date = _entry_date(cand.entry)
        cand.recent = date is not None and date >= recent_since
    ordered.sort(key=lambda c: (-c.score, -c.citations, _norm_title(c.entry.get("title"))))
    return ordered


def select(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """取前 ``limit`` 篇：约七成按融合分（不看年份），约三成留给近期工作。

    近期的不够就用其余的补满——份额是下限保证，不是配额。
    """
    if len(candidates) <= limit:
        return list(candidates)
    main = max(1, limit - int(round(limit * RECENT_SHARE)))
    picked = candidates[:main]
    chosen = {id(c) for c in picked}
    for cand in candidates[main:]:
        if len(picked) >= limit:
            break
        if cand.recent:
            picked.append(cand)
            chosen.add(id(cand))
    for cand in candidates[main:]:
        if len(picked) >= limit:
            break
        if id(cand) not in chosen:
            picked.append(cand)
    return picked


async def collect(
    adapters: dict[str, Any],
    queries: list[str],
    *,
    limit: int,
    months_back: int,
    now: datetime,
    exclude: list[str] | None = None,
    time_budget_s: float = SOURCE_TIME_BUDGET_S,
) -> SurveyResult:
    """各来源同时检索全部查询，合并排序后取前 ``limit`` 篇。

    ``adapters``：来源 id → 检索适配器（None = 这个来源现在拿不到）。每个来源有总时限
    ``time_budget_s``：到点就停，已经拿到的照用，并记成这个来源的一条错误。
    """
    out = _Collector()
    foundation_since = now - timedelta(days=365 * FOUNDATION_YEARS)
    recent_since = now - timedelta(days=30 * max(1, months_back))
    # 每条查询每个来源要多少条：总量按上限的两倍留余量（去重、不相干的会折掉一部分），
    # 这样某个来源缺席时别的来源手里也有足够的候选补上
    per_query = max(10, min(25, math.ceil(limit * 2 / max(1, len(queries)))))

    async def run(source_id: str, adapter: Any) -> None:
        kwargs = dict(foundation_since=foundation_since, now=now, per_query=per_query, out=out)
        if source_id == "arxiv":
            await _search_arxiv(adapter, queries, recent_since=recent_since, **kwargs)
        elif source_id == "openalex":
            await _search_openalex(adapter, queries, recent_since=recent_since, **kwargs)
        else:
            await _search_generic(adapter, source_id, queries, **kwargs)

    async def guarded(source_id: str, adapter: Any) -> None:
        if adapter is None:
            out.errors[source_id] = "unavailable"
            return
        try:
            await asyncio.wait_for(run(source_id, adapter), timeout=time_budget_s)
        except TimeoutError:
            out.errors.setdefault(source_id, f"timed out after {int(time_budget_s)}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 一个来源出任何错都不拖累其余
            logger.warning("survey search failed for %s", source_id, exc_info=True)
            out.errors.setdefault(source_id, f"{type(e).__name__}: {e}"[:300])

    if queries:
        await asyncio.gather(*(guarded(sid, adapter) for sid, adapter in adapters.items()))
    fused = fuse(out.lists, recent_since=recent_since, exclude=exclude)
    selected = select(fused, limit)
    by_source: dict[str, int] = {}
    for cand in selected:
        for source in cand.sources:
            by_source[source] = by_source.get(source, 0) + 1
    return SurveyResult(
        selected=selected,
        logs=out.logs,
        fetched={sid: out.fetched.get(sid, 0) for sid in adapters},
        selected_by_source=by_source,
        errors=[{"source": sid, "error": err} for sid, err in out.errors.items()],
        unique=len(fused),
    )


# ---- 按引用关系扩展 ----


async def expand_by_citations(
    openalex: Any,
    seeds: list[dict[str, Any]],
    *,
    limit: int,
    citing_per_seed: int = 8,
    time_budget_s: float = SOURCE_TIME_BUDGET_S,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从最相关的几篇出发，沿引用关系找候选：它们共同引用的（奠基工作）和引用它们的
    高被引论文。返回 (入库用的条目, 诊断)。

    ``seeds``：[{doi, arxiv_id, title}]。候选按「被几篇种子引用 / 引用了几篇种子」排，
    再按被引数——几篇相关论文都引的那篇，多半就是这个问题绕不开的工作。
    """
    stats: dict[str, Any] = {"seeds": len(seeds), "seeds_found": 0, "references": 0, "citing": 0}
    votes: dict[str, int] = {}
    citing: dict[str, dict[str, Any]] = {}
    referenced: set[str] = set()

    async def work() -> list[dict[str, Any]]:
        for seed in seeds:
            try:
                found = await openalex.find_work(
                    doi=seed.get("doi"), arxiv_id=seed.get("arxiv_id"), title=seed.get("title")
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 一篇种子查不到不影响其余
                logger.warning("citation expansion: seed lookup failed", exc_info=True)
                continue
            if not found or not found.get("openalex_id"):
                continue
            stats["seeds_found"] += 1
            for ref in found.get("referenced_works") or []:
                votes[ref] = votes.get(ref, 0) + 1
                referenced.add(ref)
            try:
                rows = await openalex.citing_works(found["openalex_id"], limit=citing_per_seed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                if row.get("openalex_id"):
                    citing[row["openalex_id"]] = row
                    votes[row["openalex_id"]] = votes.get(row["openalex_id"], 0) + 1
        stats["references"] = len(referenced)
        stats["citing"] = len(citing)
        # 参考文献只有 id：先按票数取一批最可能要的再去取详情，别把几百条全取回来
        ref_ids = sorted((r for r in votes if r not in citing), key=lambda r: -votes[r])
        details = await openalex.works_by_ids(ref_ids[: max(limit * 3, 50)])
        rows = [*details, *citing.values()]
        rows.sort(
            key=lambda r: (
                -votes.get(r.get("openalex_id") or "", 0),
                -(r.get("cited_by_count") or 0),
            )
        )
        return [entry_from_openalex(r) for r in rows if r.get("title")][:limit]

    try:
        entries = await asyncio.wait_for(work(), timeout=time_budget_s)
    except TimeoutError:
        stats["error"] = f"timed out after {int(time_budget_s)}s"
        entries = []
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — 扩展是锦上添花，失败不让检索失败
        logger.warning("citation expansion failed", exc_info=True)
        stats["error"] = f"{type(e).__name__}: {e}"[:300]
        entries = []
    stats["candidates"] = len(entries)
    return entries, stats


__all__ = [
    "Candidate",
    "QueryLog",
    "SearchQueries",
    "SurveyResult",
    "arxiv_expression",
    "collect",
    "entry_from_candidate",
    "entry_from_openalex",
    "expand_by_citations",
    "fuse",
    "merge_queries",
    "plan_queries",
    "select",
]
