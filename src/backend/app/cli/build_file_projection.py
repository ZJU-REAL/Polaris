"""全量回填常驻文件投影（#719 file-over-app 一期）。

存量数据一次性渲染进 ``<data_dir>/workspace/``（新数据由业务钩子增量刷新）::

    python -m app.cli.build_file_projection                  # 全量：PDF + 笔记 + wiki vault
    python -m app.cli.build_file_projection --library <uuid> # 只回填某个库（及其成员论文）
    python -m app.cli.build_file_projection --force          # 已存在的 PDF 投影也删掉重建
                                                             # （复制回退产生的过期副本靠它兜底）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from contextlib import suppress

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperHighlight, PaperNote
from app.services import file_projection

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=uuid.UUID, help="只回填该方向库（vault + 成员论文）")
    parser.add_argument(
        "--force", action="store_true", help="已存在的 PDF 投影也删掉重建（默认幂等跳过）"
    )
    return parser


def _member_paper_ids(library_id: uuid.UUID):
    return select(LibraryPaper.paper_id).where(LibraryPaper.library_id == library_id)


async def _run(args: argparse.Namespace) -> None:
    if not file_projection.projection_enabled():
        print("文件投影开关已关（POLARIS_FILE_PROJECTION=0），先打开再回填。")
        raise SystemExit(1)
    stats = {"pdfs": 0, "pdf_skipped": 0, "notes": 0, "vaults": 0, "failed": 0}
    try:
        async with get_sessionmaker()() as session:
            # ---- PDF 别名 ----
            stmt = select(Paper).where(Paper.pdf_path.is_not(None)).order_by(Paper.created_at)
            if args.library:
                stmt = stmt.where(Paper.id.in_(_member_paper_ids(args.library)))
            papers = (await session.execute(stmt)).scalars().all()
            pdf_dir = file_projection.workspace_root() / file_projection.PDF_DIR
            for i, paper in enumerate(papers, start=1):
                try:
                    if args.force:
                        with suppress(OSError):
                            (pdf_dir / file_projection.paper_pdf_filename(paper)).unlink(
                                missing_ok=True
                            )
                    if file_projection.project_paper_pdf(paper) is not None:
                        stats["pdfs"] += 1
                    else:
                        stats["pdf_skipped"] += 1  # 原件缺失 / 投影失败（细节见日志）
                except Exception:  # noqa: BLE001 — 单篇失败不拖垮整批
                    logger.warning("pdf projection failed for %s", paper.id, exc_info=True)
                    stats["failed"] += 1
                if i % 100 == 0 or i == len(papers):
                    print(f"papers: {i}/{len(papers)}")

            # ---- 笔记 / 划线 ----
            note_ids = select(PaperNote.paper_id).union(select(PaperHighlight.paper_id))
            if args.library:
                note_ids = (
                    select(PaperNote.paper_id)
                    .where(PaperNote.paper_id.in_(_member_paper_ids(args.library)))
                    .union(
                        select(PaperHighlight.paper_id).where(
                            PaperHighlight.paper_id.in_(_member_paper_ids(args.library))
                        )
                    )
                )
            paper_ids = [pid for (pid,) in (await session.execute(note_ids)).all()]
            for i, paper_id in enumerate(paper_ids, start=1):
                try:
                    await file_projection.refresh_paper_notes(session, paper_id)
                    stats["notes"] += 1
                except Exception:  # noqa: BLE001
                    logger.warning("notes projection failed for %s", paper_id, exc_info=True)
                    stats["failed"] += 1
                if i % 100 == 0 or i == len(paper_ids):
                    print(f"notes: {i}/{len(paper_ids)}")

            # ---- wiki vault（跳过没有已编译/已收录成员的空库，同全量导出口径）----
            lib_stmt = select(DirectionLibrary).order_by(DirectionLibrary.created_at)
            if args.library:
                lib_stmt = lib_stmt.where(DirectionLibrary.id == args.library)
            libraries = (await session.execute(lib_stmt)).scalars().all()
            for library in libraries:
                has_members = (
                    await session.execute(
                        select(LibraryPaper.paper_id)
                        .where(
                            LibraryPaper.library_id == library.id,
                            LibraryPaper.status.in_(("compiled", "included")),
                        )
                        .limit(1)
                    )
                ).first() is not None
                if not has_members:
                    continue
                try:
                    await file_projection.refresh_library_vault(session, library)
                    stats["vaults"] += 1
                    print(f"vault: {library.name}")
                except Exception:  # noqa: BLE001
                    logger.warning("vault projection failed for %s", library.id, exc_info=True)
                    stats["failed"] += 1
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
