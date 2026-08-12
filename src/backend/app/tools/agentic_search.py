"""Agentic search 的两个原语：可筛选的宽扫 + 字面 grep。

Claude Code 的检索之所以好用，不在于"检索得准"，而在于**把宽度和深度拆成两轮**：
先 grep 撒一网（每条命中就一行，可以看几十条），再对少数目标 read 完整内容。

平台原有的 ``search_papers`` / ``search_chunks`` 都是一步到位的 top-k：一次调用就把
片段正文塞回来，k 稍微大一点上下文就满了，所以模型只敢要 5 条——那等于回到了 RAG。
这里补上缺的那一半：

- :func:`scan_papers`：检索或按元数据筛选论文，返回紧凑元数据。k 可以到 50。
- :func:`grep_fulltext`：字面匹配全文，返回**命中的那几行**而不是整段，带论文 id。

深读仍然用现成的 ``read_fulltext(paper_id, query)``。
"""

import datetime as dt
import re
import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.services import chunks as chunks_service
from app.services import papers as papers_service
from app.services.embedding import embed_query
from app.tools.context import ToolContext
from app.tools.registry import tool
from app.tools.scope import library_ids_for

#: grep 每条命中回多少字符的上下文（前后各一半）
_GREP_WINDOW = 160
#: 每篇论文最多回几条命中，免得一篇刷屏
_GREP_PER_PAPER = 3
_MAX_SCAN = 50
_SCAN_STATUSES = (
    "all",
    "visible",
    "library",
    "pending_compile",
    "compiled_any",
    "candidate",
    "scored",
    "excluded",
    "fetched",
    "compiled",
    "included",
)
_PRECISE_FILTERS = frozenset(
    {
        "status",
        "author",
        "affiliation",
        "published_from",
        "published_to",
        "created_from",
        "created_to",
        "tag",
        "my_tag",
        "starred",
        "reading_status",
        "daily_only",
        "last_sync_only",
        "sort",
        "page",
    }
)


@tool(
    name="scan_papers",
    description=(
        "宽扫或精确筛选当前项目文献库中的论文，不返回摘要。query 可省略；"
        "支持限定某个已关联文献库，并按作者、发表机构、发表时间、入库时间、"
        "状态、标签和个人阅读状态筛选，按相关性或时间排序和分页。"
        "建议先用它铺开语料，再用 read_fulltext 或 read_wiki 深读少数论文。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "可选检索词。单独使用时支持语义检索；与精确筛选一起使用时匹配标题或摘要"
                ),
            },
            "library_id": {
                "type": "string",
                "format": "uuid",
                "description": "只扫描当前项目已关联的这个文献库；省略则扫描关联库并集",
            },
            "mode": {
                "type": "string",
                "enum": ["keyword", "semantic"],
                "default": "semantic",
                "description": "仅 query 单独使用时生效；精确筛选使用确定性元数据查询",
            },
            "status": {
                "type": "string",
                "enum": list(_SCAN_STATUSES),
                "default": "library",
                "description": "论文状态或状态组；all 包含候选和已排除论文",
            },
            "author": {"type": "string", "description": "作者姓名，忽略大小写的包含匹配"},
            "affiliation": {
                "type": "string",
                "description": "发表机构名称，忽略大小写的包含匹配",
            },
            "published_from": {
                "type": "string",
                "description": "发表时间下界，ISO 8601 日期或时间，包含边界",
            },
            "published_to": {
                "type": "string",
                "description": "发表时间上界，ISO 8601 日期或时间，包含边界",
            },
            "created_from": {
                "type": "string",
                "description": "入库时间下界，ISO 8601 日期或时间，包含边界",
            },
            "created_to": {
                "type": "string",
                "description": "入库时间上界，ISO 8601 日期或时间，包含边界",
            },
            "tag": {"type": "string", "description": "按文献库共享标签精确匹配"},
            "my_tag": {"type": "string", "description": "按当前用户的个人标签精确匹配"},
            "starred": {"type": "boolean", "description": "按当前用户是否星标筛选"},
            "reading_status": {
                "type": "string",
                "enum": ["unread", "reading", "read"],
                "description": "按当前用户阅读状态筛选",
            },
            "daily_only": {"type": "boolean", "description": "只看每日论文池自动收录的论文"},
            "last_sync_only": {
                "type": "boolean",
                "description": "只看文献库最近一次同步新增的论文",
            },
            "sort": {
                "type": "string",
                "enum": list(papers_service.PAPER_SORTS),
                "default": "relevance",
                "description": (
                    "relevance 按相关性；published_at 和 created_at 为时间升序，前缀负号为降序"
                ),
            },
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_SCAN,
                "default": 30,
                "description": "每页最多返回几篇，默认 30，上限 50",
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_SCAN,
                "description": "limit 的兼容别名；两者同时提供时使用 limit",
            },
        },
    },
    summarize=lambda a, r: (
        f"宽扫 {a.get('query') or '文献库'} → {len(r.get('results') or [])} 篇"
    ),
)
async def scan_papers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    limit = _bounded_int(args.get("limit", args.get("k", 30)), "limit", 1, _MAX_SCAN)
    page = _bounded_int(args.get("page", 1), "page", 1, 1_000_000)
    mode = str(args.get("mode") or "semantic")
    if mode not in {"keyword", "semantic"}:
        raise ValueError("mode 只能是 keyword 或 semantic")

    async with get_sessionmaker()() as session:
        library_id, library_ids = await _scan_library_scope(session, ctx, args.get("library_id"))
        precise = not query or any(name in args for name in _PRECISE_FILTERS)
        if precise:
            return await _scan_filtered(
                session,
                ctx,
                args,
                query=query,
                library_id=library_id,
                library_ids=library_ids,
                page=page,
                limit=limit,
            )

        rows: list[tuple[Any, float]] = []
        used = "keyword"
        if mode == "semantic" and papers_service.semantic_search_supported(session):
            try:
                vector, space = await embed_query(
                    session,
                    query,
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    voyage_id=ctx.voyage_id,
                )
                rows = await papers_service.semantic_search_papers(
                    session,
                    project_id=ctx.project_id,
                    library_ids=library_ids,
                    query_vector=vector,
                    space=space,
                    limit=limit,
                )
                used = "semantic"
            except Exception:  # noqa: BLE001 — 向量不可用就降级，别让宽扫整个失败
                rows = []
        if not rows:
            rows = await papers_service.keyword_search_papers(
                session,
                project_id=ctx.project_id,
                library_ids=library_ids,
                q=query,
                limit=limit,
                user_id=ctx.user_id,
            )
            used = "keyword"
        # 刻意不回 tldr/摘要：回了就又变成 top-k RAG，limit 也不敢往大了要。
        return {
            "mode": used,
            "count": len(rows),
            "results": [_scan_brief(p) for p, _ in rows],
        }


def _bounded_int(raw: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{name} 需要整数")
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 需要整数") from e
    if value < minimum or value > maximum:
        raise ValueError(f"{name} 需要在 {minimum} 到 {maximum} 之间")
    return value


async def _scan_library_scope(
    session: Any, ctx: ToolContext, raw_library_id: Any
) -> tuple[uuid.UUID | None, list[uuid.UUID]]:
    linked_ids = await library_ids_for(session, ctx)
    if raw_library_id in (None, ""):
        return None, linked_ids
    try:
        library_id = uuid.UUID(str(raw_library_id))
    except (TypeError, ValueError) as e:
        raise ValueError(f"library_id 不是合法 uuid：{raw_library_id}") from e
    if library_id not in linked_ids:
        raise ValueError(
            "该文献库未关联到当前项目或无权访问；请先调用 "
            "list_libraries(linked_only=true) 获取可扫描的文献库"
        )
    return library_id, [library_id]


def _parse_datetime(raw: Any, name: str, *, end_of_day: bool = False) -> dt.datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            boundary = dt.time.max if end_of_day else dt.time.min
            return dt.datetime.combine(dt.date.fromisoformat(value), boundary, tzinfo=dt.UTC)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{name} 需要 ISO 8601 日期或时间：{value}") from e
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _optional_bool(args: dict[str, Any], name: str) -> bool | None:
    if name not in args:
        return None
    value = args[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} 需要布尔值")
    return value


async def _scan_filtered(
    session: Any,
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    query: str,
    library_id: uuid.UUID | None,
    library_ids: list[uuid.UUID],
    page: int,
    limit: int,
) -> dict[str, Any]:
    status = str(args.get("status") or "library")
    if status not in _SCAN_STATUSES:
        raise ValueError(f"status 不受支持：{status}")
    sort = str(args.get("sort") or "relevance")
    if sort not in papers_service.PAPER_SORTS:
        raise ValueError(f"sort 不受支持：{sort}")

    published_from = _parse_datetime(args.get("published_from"), "published_from")
    published_to = _parse_datetime(
        args.get("published_to"), "published_to", end_of_day=True
    )
    created_from = _parse_datetime(args.get("created_from"), "created_from")
    created_to = _parse_datetime(args.get("created_to"), "created_to", end_of_day=True)
    for lower, upper, label in (
        (published_from, published_to, "发表时间"),
        (created_from, created_to, "入库时间"),
    ):
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f"{label}下界不能晚于上界")

    user_filters = any(name in args for name in ("my_tag", "starred", "reading_status"))
    if user_filters and ctx.user_id is None:
        raise ValueError("个人标签、星标和阅读状态筛选需要用户身份")
    reading_status = str(args.get("reading_status") or "").strip() or None
    if reading_status not in {None, "unread", "reading", "read"}:
        raise ValueError("reading_status 只能是 unread、reading 或 read")

    rows, total = await papers_service.list_papers(
        session,
        project_id=ctx.project_id,
        library_id=library_id,
        library_ids=None if library_id is not None else library_ids,
        status=None if status == "all" else status,
        q=query or None,
        tag=str(args.get("tag") or "").strip() or None,
        my_tag=str(args.get("my_tag") or "").strip() or None,
        starred=_optional_bool(args, "starred"),
        reading_status=reading_status,
        user_id=ctx.user_id,
        sort=sort,
        page=page,
        size=limit,
        author=str(args.get("author") or "").strip() or None,
        affiliation=str(args.get("affiliation") or "").strip() or None,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
        daily_only=_optional_bool(args, "daily_only") is True,
        last_sync_only=_optional_bool(args, "last_sync_only") is True,
    )
    has_more = page * limit < total
    return {
        "mode": "filtered",
        "count": len(rows),
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "sort": sort,
        "results": [_scan_brief(p) for p in rows],
    }


def _scan_brief(paper: Any) -> dict[str, Any]:
    author_names: list[str] = []
    for item in paper.authors or []:
        if isinstance(item, str) and item:
            author_names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            author_names.append(str(item["name"]))
    affiliations = [str(item) for item in (paper.affiliations or []) if item]
    return {
        "paper_id": str(paper.id),
        "library_id": str(paper.library_id) if paper.library_id else None,
        "title": paper.title,
        "year": paper.year,
        "published_at": paper.published_at.isoformat() if paper.published_at else None,
        "created_at": paper.created_at.isoformat(),
        "authors": author_names[:20],
        "author_count": len(author_names),
        "affiliations": affiliations[:10],
        "affiliation_count": len(affiliations),
        "venue": paper.venue,
        "status": paper.status,
        "relevance_score": paper.relevance_score,
        "has_wiki": paper.has_wiki,
        "has_fulltext": bool(paper.full_text_path),
    }


@tool(
    name="grep_fulltext",
    description=(
        "在论文全文里做字面匹配，返回命中处前后的一小段文本，而不是整个片段。"
        "适合查找确切的术语、模型名、数据集名或公式符号，这类查询上语义检索的准确率反而更低。"
        "拿到 paper_id 之后用 read_fulltext 查看完整上下文。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "要找的字面串（不是正则）"},
            "k": {"type": "integer", "description": "最多返回几条命中（默认 20，上限 40）"},
        },
        "required": ["pattern"],
    },
    summarize=lambda a, r: f"grep {a.get('pattern')} → {r.get('count', 0)} 处",
)
async def grep_fulltext(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return {"error": "缺少 pattern"}
    k = max(1, min(int(args.get("k") or 20), 40))

    async with get_sessionmaker()() as session:
        library_ids = await library_ids_for(session, ctx)
        rows = await chunks_service.keyword_search_chunks(
            session,
            library_ids=library_ids or None,
            q=pattern,
            limit=k * 4,  # 多取一些，下面按论文去重后再截断
        )
        titles = await _titles(session, [c.paper_id for c, _ in rows])

    hits: list[dict[str, Any]] = []
    per_paper: dict[Any, int] = {}
    lowered = pattern.lower()
    for chunk, _score in rows:
        if len(hits) >= k:
            break
        seen = per_paper.get(chunk.paper_id, 0)
        if seen >= _GREP_PER_PAPER:
            continue
        for line in _matching_windows(chunk.text or "", lowered):
            hits.append(
                {
                    "paper_id": str(chunk.paper_id),
                    "title": titles.get(chunk.paper_id),
                    "excerpt": line,
                }
            )
            per_paper[chunk.paper_id] = seen + 1
            break
    return {"pattern": pattern, "count": len(hits), "hits": hits}


async def _titles(session: Any, paper_ids: list[Any]) -> dict[Any, str]:
    from sqlalchemy import select

    from app.models.paper import Paper

    if not paper_ids:
        return {}
    rows = await session.execute(
        select(Paper.id, Paper.title).where(Paper.id.in_(list(set(paper_ids))))
    )
    return dict(rows.all())


def _matching_windows(text: str, lowered_pattern: str) -> list[str]:
    """命中处前后各截一段，而不是把整个 chunk 回过去。

    整段回等于把 grep 变回了 RAG——一次调用几千字符，k 就再也大不起来。
    """
    out: list[str] = []
    haystack = text.lower()
    start = haystack.find(lowered_pattern)
    if start < 0:
        return out
    half = _GREP_WINDOW // 2
    left = max(0, start - half)
    right = min(len(text), start + len(lowered_pattern) + half)
    excerpt = text[left:right].strip().replace("\n", " ")
    out.append(re.sub(r"\s+", " ", excerpt))
    return out
