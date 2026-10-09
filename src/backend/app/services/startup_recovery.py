"""启动时收拾上一次进程留下的半截活（#850）。

引擎是单进程：后台任务都是本进程里的 asyncio 任务。进程被杀（退出、崩溃、断电）时，
那些任务写到一半的「进行中」状态就再也没人推进了——而有些状态会一直挡路：一条卡在
running 的定时文献发现，会让 claim_due_schedules 对那个库永远报 RUN_ALREADY_ACTIVE。

启动时（还没有任何任务在跑）把只由进程内任务推进的「进行中」行一律标成失败，
写明原因，用户可以照常重试：

- 文献发现运行（LiteratureSearchRun running → failed）及其来源尝试；
  queued 的不动——手动建的运行本来就可以先排着等用户点开始，定时的到点会被重新派发；
- 文献条目翻译（queued/running → failed；再次请求翻译即重新入队）；
- PDF 解析版本（非终态 → failed，向量构建中 → failed；可再发起一次解析）；
- 开放获取 PDF 缓存（downloading → failed；下次会重新下载）；
- Zotero 导入与全量导出的状态只在进程内 redis 里，随进程消失；留下的只有暂存目录，
  这里一并清掉。

航程不在这里：reconcile_stuck_voyages 会把在途航程重新排上续跑。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

INTERRUPTED_CODE = "INTERRUPTED"
INTERRUPTED_MESSAGE = "interrupted when Polaris quit"

#: 解析版本的终态（其余状态都只可能由一个在跑的解析任务推进）
CONTENT_TERMINAL_STATUSES = ("ready", "ready_fallback", "vector_ready", "failed")


async def reset_interrupted_work(session: AsyncSession) -> dict[str, int]:
    """把上一次进程留下的「进行中」行标成失败；返回每类改了几行。"""
    from app.models.literature_discovery import (
        LiteratureHitTranslation,
        LiteratureOaAttempt,
        LiteratureOaCache,
        LiteratureSearchRun,
        LiteratureSourceAttempt,
    )
    from app.models.paper_content import PaperContentVersion

    now = datetime.now(UTC)
    counts: dict[str, int] = {}

    async def _apply(name: str, stmt) -> None:
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        counts[name] = int(result.rowcount or 0)

    await _apply(
        "literature_source_attempts",
        update(LiteratureSourceAttempt)
        .where(
            LiteratureSourceAttempt.status.in_(("pending", "running")),
            LiteratureSourceAttempt.run_id.in_(
                select(LiteratureSearchRun.id).where(LiteratureSearchRun.status == "running")
            ),
        )
        .values(
            status="failed",
            error_code=INTERRUPTED_CODE,
            error_detail=INTERRUPTED_MESSAGE,
            completed_at=now,
        ),
    )
    await _apply(
        "literature_search_runs",
        update(LiteratureSearchRun)
        .where(LiteratureSearchRun.status == "running")
        .values(status="failed", error_summary=INTERRUPTED_CODE, completed_at=now),
    )
    await _apply(
        "literature_hit_translations",
        update(LiteratureHitTranslation)
        .where(LiteratureHitTranslation.status.in_(("queued", "running")))
        .values(status="failed", error_code=INTERRUPTED_CODE, completed_at=now),
    )
    await _apply(
        "literature_oa_attempts",
        update(LiteratureOaAttempt)
        .where(LiteratureOaAttempt.status == "running")
        .values(status="failed", error_code=INTERRUPTED_CODE, error_detail=INTERRUPTED_MESSAGE),
    )
    await _apply(
        "literature_oa_caches",
        update(LiteratureOaCache)
        .where(LiteratureOaCache.status.in_(("downloading",)))
        .values(status="failed", error_code=INTERRUPTED_CODE, error_detail=INTERRUPTED_MESSAGE),
    )
    await _apply(
        "paper_content_versions",
        update(PaperContentVersion)
        .where(PaperContentVersion.status.not_in(CONTENT_TERMINAL_STATUSES))
        .values(
            status="failed",
            error_code=INTERRUPTED_CODE,
            error_detail=INTERRUPTED_MESSAGE,
        ),
    )
    await _apply(
        "paper_content_document_vectors",
        update(PaperContentVersion)
        .where(PaperContentVersion.document_vector_state == "building")
        .values(document_vector_state="failed"),
    )
    await _apply(
        "paper_content_chunk_vectors",
        update(PaperContentVersion)
        .where(PaperContentVersion.chunk_vector_state == "building")
        .values(chunk_vector_state="failed"),
    )
    await session.commit()
    touched = {k: v for k, v in counts.items() if v}
    if touched:
        logger.warning("reset work interrupted by the last shutdown: %s", touched)
    return counts


def _clear_staging_dirs(data_dir: Path) -> int:
    """清掉没跑完的 Zotero 导入暂存目录与全量导出工作目录（成品 zip 保留）。"""
    removed = 0
    for root in (data_dir / "zotero_imports", data_dir / "exports"):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


async def recover_interrupted_work() -> None:
    """lifespan 启动时调用；任何一步失败只记日志，不拦启动。"""
    from app.core.config import get_settings
    from app.core.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            await reset_interrupted_work(session)
    except Exception:  # noqa: BLE001 — 收拾不了旧摊子不该让引擎起不来
        logger.warning("resetting interrupted work failed", exc_info=True)
    try:
        removed = await asyncio.to_thread(_clear_staging_dirs, Path(get_settings().data_dir))
        if removed:
            logger.info("removed %d leftover staging dir(s)", removed)
    except Exception:  # noqa: BLE001
        logger.warning("clearing leftover staging dirs failed", exc_info=True)
