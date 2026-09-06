"""全库批量回填（#661）：schema 引导的论文骨架抽取。

存量论文一次性覆盖用（新论文由补全钩子增量处理）::

    python -m app.cli.backfill_extractions                 # 通用骨架，跳过已抽取
    python -m app.cli.backfill_extractions --limit 100
    python -m app.cli.backfill_extractions --force         # 覆盖重抽（schema 升版后用）
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
from app.services.extraction.runtime import DEFAULT_SCHEMA_ID, extract_paper
from app.services.extraction.schemas import get_schema

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema", default=DEFAULT_SCHEMA_ID, help="抽取 schema id（默认通用骨架 skeleton）"
    )
    parser.add_argument("--limit", type=int, help="只处理最早入库的前 N 篇")
    parser.add_argument(
        "--force", action="store_true", help="已有产物也覆盖重抽（schema 升版后回填用）"
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    stats = {"papers": 0, "extracted": 0, "skipped": 0, "failed": 0}
    try:
        # schema 名写错是调用方错误：进循环前就炸掉，别把每篇都记成 failed
        get_schema(args.schema)
        llm = get_llm_router()
        async with get_sessionmaker()() as session:
            stmt = select(Paper).order_by(Paper.created_at)
            if args.limit:
                stmt = stmt.limit(args.limit)
            papers = (await session.execute(stmt)).scalars().all()
            for paper in papers:
                stats["papers"] += 1
                try:
                    outcome = await extract_paper(
                        session, paper, schema_id=args.schema, llm=llm, force=args.force
                    )
                    if outcome.status == "extracted":
                        await session.commit()
                        stats["extracted"] += 1
                    else:
                        stats["skipped"] += 1
                        logger.info("skipped %s: %s", paper.id, outcome.reason)
                except Exception:  # noqa: BLE001 — 单篇失败不拖垮整批
                    logger.warning("extraction backfill failed for %s", paper.id, exc_info=True)
                    await session.rollback()
                    stats["failed"] += 1
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
