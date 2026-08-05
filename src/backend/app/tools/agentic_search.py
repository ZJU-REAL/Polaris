"""Agentic search 的两个原语：廉价宽扫 + 字面 grep。

Claude Code 的检索之所以好用，不在于"检索得准"，而在于**把宽度和深度拆成两轮**：
先 grep 撒一网（每条命中就一行，可以看几十条），再对少数目标 read 完整内容。

平台原有的 ``search_papers`` / ``search_chunks`` 都是一步到位的 top-k：一次调用就把
片段正文塞回来，k 稍微大一点上下文就满了，所以模型只敢要 5 条——那等于回到了 RAG。
这里补上缺的那一半：

- :func:`scan_papers`：每篇只回 ``id + 标题 + 年份``，一行。k 可以到 50。
- :func:`grep_fulltext`：字面匹配全文，返回**命中的那几行**而不是整段，带论文 id。

深读仍然用现成的 ``read_fulltext(paper_id, query)``。
"""

import re
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


@tool(
    name="scan_papers",
    description=(
        "宽扫：按查询词列出可能相关的论文，每篇只给标题和年份，不给摘要。"
        "开销很小，可以一次要几十篇。建议先用它铺开看语料里有什么，"
        "再用 read_fulltext 或 read_wiki 对其中少数几篇深读。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "k": {"type": "integer", "description": "最多返回几篇（默认 30，上限 50）"},
            "mode": {"type": "string", "enum": ["keyword", "semantic"]},
        },
        "required": ["query"],
    },
    summarize=lambda a, r: f"宽扫 {a.get('query')} → {len(r.get('results') or [])} 篇",
)
async def scan_papers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "缺少 query"}
    k = max(1, min(int(args.get("k") or 30), 50))
    mode = str(args.get("mode") or "semantic")

    async with get_sessionmaker()() as session:
        rows: list[tuple[Any, float]] = []
        used = "keyword"
        if mode == "semantic" and papers_service.semantic_search_supported(session):
            try:
                vector, space = await embed_query(
                    session, query, user_id=ctx.user_id, project_id=ctx.project_id
                )
                rows = await papers_service.semantic_search_papers(
                    session,
                    project_id=ctx.project_id,
                    library_ids=await library_ids_for(session, ctx),
                    query_vector=vector,
                    space=space,
                    limit=k,
                )
                used = "semantic"
            except Exception:  # noqa: BLE001 — 向量不可用就降级，别让宽扫整个失败
                rows = []
        if not rows:
            rows = await papers_service.keyword_search_papers(
                session,
                project_id=ctx.project_id,
                library_ids=await library_ids_for(session, ctx),
                q=query,
                limit=k,
            )
        # 一行一篇：标题 + 年份。**刻意不回 tldr/摘要**——回了就又变成 top-k RAG，
        # k 也就不敢往大了要。
        return {
            "mode": used,
            "count": len(rows),
            "results": [
                {"paper_id": str(p.id), "title": p.title, "year": p.year} for p, _ in rows
            ],
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
