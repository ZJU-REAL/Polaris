"""全库批量回填（#639）：引文边构建 + 引文意图分类 + OpenAlex 对齐。

存量论文一次性覆盖用（新论文由补全钩子增量处理）::

    python -m app.cli.backfill_citations            # 全部三步
    python -m app.cli.backfill_citations --skip-align --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.core.llm.router import get_llm_router
from app.models.paper import Paper

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-intents", action="store_true", help="只建边不做意图分类")
    parser.add_argument("--skip-align", action="store_true", help="不做 OpenAlex 对齐")
    parser.add_argument(
        "--force-edges", action="store_true", help="重建已有引文边（丢弃已分类的意图）"
    )
    parser.add_argument("--limit", type=int, help="只处理最早入库的前 N 篇")
    return parser


async def _run(args: argparse.Namespace) -> None:
    stats = {"papers": 0, "edges": 0, "classified": 0, "aligned": 0, "failed": 0}
    try:
        from app.services.citation_graph import (
            classify_citation_intents,
            ensure_citation_edges,
        )

        llm = get_llm_router()
        async with get_sessionmaker()() as session:
            stmt = select(Paper).order_by(Paper.created_at)
            if args.limit:
                stmt = stmt.limit(args.limit)
            papers = (await session.execute(stmt)).scalars().all()
            for paper in papers:
                stats["papers"] += 1
                try:
                    edges = await ensure_citation_edges(
                        session, paper, force=args.force_edges
                    )
                    if edges:
                        await session.commit()
                        stats["edges"] += edges
                    if not args.skip_intents:
                        classified = await classify_citation_intents(session, paper, llm)
                        if classified:
                            await session.commit()
                            stats["classified"] += classified
                except Exception:  # noqa: BLE001 — 单篇失败不拖垮整批
                    logger.warning("citation backfill failed for %s", paper.id, exc_info=True)
                    await session.rollback()
                    stats["failed"] += 1
            if not args.skip_align:
                from app.services.openalex_align import align_missing_papers

                align_stats = await align_missing_papers(session, limit=args.limit)
                stats["aligned"] = align_stats["aligned"]
                stats["failed"] += align_stats["failed"]
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
