"""全库批量回填（#661/#663）：schema 引导的论文结构化抽取。

存量论文一次性覆盖用（新论文由补全钩子增量处理）::

    python -m app.cli.backfill_extractions                 # 按每篇论文所在库的学科定口径
    python -m app.cli.backfill_extractions --schema method # 只回填方法卡
    python -m app.cli.backfill_extractions --schema gaps   # 只回填缺口与负结果台账（#665）
    python -m app.cli.backfill_extractions --limit 100
    python -m app.cli.backfill_extractions --force         # 覆盖重抽（schema 升版后用）

方法卡（method 或学科包的 ``<包名>.method``）抽到后顺手刷双轴向量索引
（services/method_index.py）；provider 不支持嵌入时索引跳过，抽取产物照常落表。

**口径按库的学科走，不是注册表全量**（#770）：装了一个学科包，不该让别的学科的论文
也多跑一次抽取——那是每篇论文一次白烧的 LLM 调用，且产出的卡用错了领域的字段。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.services.extraction.runtime import extract_paper
from app.services.extraction.schemas import ExtractionSchema, get_schema, schemas_for
from app.services.method_index import is_method_schema_id, refresh_paper_method_index

logger = logging.getLogger(__name__)


async def _library_contexts(
    session, paper_id: uuid.UUID
) -> list[tuple[uuid.UUID | None, str | None]]:
    """这篇论文所在的库及其学科；不属于任何库时返回一个空上下文。

    论文是全平台共享的内容池，库的边界在 library_papers 上——所以「该用哪个学科的
    口径」只能顺着成员行反查，不能从 papers 表读。个人书架导入的论文没有成员行，
    对应 ``(None, None)``：只抽跨学科通用的内置 schema，与学科包出现之前一致。
    """
    rows = (
        await session.execute(
            select(DirectionLibrary.id, DirectionLibrary.discipline)
            .join(LibraryPaper, LibraryPaper.library_id == DirectionLibrary.id)
            .where(LibraryPaper.paper_id == paper_id)
            .order_by(DirectionLibrary.discipline.is_(None), DirectionLibrary.id)
        )
    ).all()
    return [(row[0], row[1]) for row in rows] or [(None, None)]


def _plan(
    contexts: list[tuple[uuid.UUID | None, str | None]],
) -> list[tuple[ExtractionSchema, uuid.UUID | None]]:
    """这篇论文这一轮要抽哪些 schema，各自记在哪个库名下。

    同一篇可能同时在几个库里（各自学科不同），并集而不是二选一：每个库都该拿到它那
    一版的卡。内置 schema 在每个库的口径里都有，按 id 去重只抽一次。
    """
    planned: dict[str, tuple[ExtractionSchema, uuid.UUID | None]] = {}
    for library_id, discipline in contexts:
        for schema in schemas_for(discipline):
            planned.setdefault(schema.id, (schema, library_id))
    return sorted(planned.values(), key=lambda item: item[0].id)


def _index_library(
    contexts: list[tuple[uuid.UUID | None, str | None]],
) -> uuid.UUID | None:
    """刷双轴索引时报哪个库。

    双轴向量按论文存、不带 library_id（models/vectors.py），所以多库论文只能有一份。
    报一个**声明了学科**的库：方法卡的两根轴在该学科口径下更具体，而内置卡在任何
    库里都取得到。都没声明学科时报第一个库，等价于旧行为。
    """
    for library_id, discipline in contexts:
        if discipline:
            return library_id
    return contexts[0][0] if contexts else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="all",
        help="抽取 schema id（默认 all = 按每篇论文所在库的学科定口径）",
    )
    parser.add_argument("--limit", type=int, help="只处理最早入库的前 N 篇")
    parser.add_argument(
        "--force", action="store_true", help="已有产物也覆盖重抽（schema 升版后回填用）"
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    stats = {"papers": 0, "extracted": 0, "skipped": 0, "failed": 0, "indexed": 0}
    try:
        # schema 名写错是调用方错误：进循环前就炸掉，别把每篇都记成 failed
        explicit = None if args.schema == "all" else get_schema(args.schema)
        llm = get_llm_router()
        async with get_sessionmaker()() as session:
            stmt = select(Paper).order_by(Paper.created_at)
            if args.limit:
                stmt = stmt.limit(args.limit)
            papers = (await session.execute(stmt)).scalars().all()
            for paper in papers:
                stats["papers"] += 1
                contexts = await _library_contexts(session, paper.id)
                index_library = _index_library(contexts)
                plan = (
                    [(explicit, index_library)] if explicit is not None else _plan(contexts)
                )
                method_extracted = False
                for schema, library_id in plan:
                    try:
                        outcome = await extract_paper(
                            session,
                            paper,
                            schema_id=schema.id,
                            llm=llm,
                            force=args.force,
                            library_id=library_id,
                        )
                        if outcome.status == "extracted":
                            await session.commit()
                            stats["extracted"] += 1
                            # 学科卡（``<包名>.method``）和内置 method 一样要进索引，
                            # 否则抽出来了却检索不到，而且不报错
                            method_extracted |= is_method_schema_id(schema.id)
                        else:
                            stats["skipped"] += 1
                            logger.info(
                                "skipped %s (%s): %s", paper.id, schema.id, outcome.reason
                            )
                    except Exception:  # noqa: BLE001 — 单篇/单 schema 失败不拖垮整批
                        logger.warning(
                            "extraction backfill failed for %s (%s)",
                            paper.id,
                            schema.id,
                            exc_info=True,
                        )
                        await session.rollback()
                        stats["failed"] += 1
                # 一篇刷一次：两张方法卡都抽到时循环里刷两遍等于白烧一次嵌入，
                # 结果还被后一遍覆盖
                if method_extracted:
                    try:
                        if await refresh_paper_method_index(
                            session, paper, library_id=index_library
                        ):
                            await session.commit()
                            stats["indexed"] += 1
                    except NotImplementedError:
                        await session.rollback()
                        logger.info("method index skipped: embeddings unsupported")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
