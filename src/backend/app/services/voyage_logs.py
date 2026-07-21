"""任务终端日志持久化：把结构化日志行与大模型完整输出落库，供刷新后 / 事后回看。

- 只记录 ``log`` 事件与 ``llm`` 完整输出（不记高频 llm_delta，实时增量仍走 SSE）。
- 尽力而为：任何失败只 log warning，绝不影响任务主流程（照 core/llm/call_log.py 范式）。
- 每次写入时低频（默认 10 分钟一次）顺带删除保留期外的旧日志。
- 读取上限与前端终端 TERMINAL_MAX 对齐，只回放最近若干条。
"""

import logging
import time
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.core.db import get_sessionmaker
from app.models.base import utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.voyage import VoyageTerminalLog

logger = logging.getLogger(__name__)

MESSAGE_MAX_CHARS = 50_000  # 单条落库文本上限（大模型长输出截断）
RETENTION_DAYS = 30
DEFAULT_READ_LIMIT = 3_000  # 回放上限（与前端 TERMINAL_MAX 对齐）
_CLEANUP_INTERVAL = 600.0  # 保留期清理最小间隔（秒）

_last_cleanup_at: float | None = None


def reset_state() -> None:
    """测试用：清空清理节流状态。"""
    global _last_cleanup_at
    _last_cleanup_at = None


def _truncate(text: str) -> str:
    if len(text) <= MESSAGE_MAX_CHARS:
        return text
    return text[:MESSAGE_MAX_CHARS] + f"\n…[truncated, {len(text)} chars total]"


async def record_terminal_log(
    run_id: uuid.UUID | str,
    event: str,
    *,
    message: str,
    level: str | None = None,
    stage: str | None = None,
) -> None:
    """写一条终端日志（尽力而为，失败只 warning）；低频顺带清理过期日志。"""
    global _last_cleanup_at
    if not message:
        return
    try:
        from app.models.voyage import VoyageTerminalLog

        run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        async with get_sessionmaker()() as session:
            now = time.monotonic()
            if _last_cleanup_at is None or now - _last_cleanup_at >= _CLEANUP_INTERVAL:
                _last_cleanup_at = now
                await session.execute(
                    delete(VoyageTerminalLog).where(
                        VoyageTerminalLog.at < utcnow() - timedelta(days=RETENTION_DAYS)
                    )
                )
            session.add(
                VoyageTerminalLog(
                    run_id=run_uuid,
                    event=event,
                    level=level,
                    stage=stage,
                    message=_truncate(message),
                    at=utcnow(),
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — 日志记录绝不影响任务主流程
        logger.warning(
            "voyage terminal log write failed (run=%s event=%s)", run_id, event, exc_info=True
        )


async def fetch_terminal_logs(
    session: "AsyncSession", run_id: uuid.UUID, *, limit: int = DEFAULT_READ_LIMIT
) -> list["VoyageTerminalLog"]:  # noqa: F821 — 延迟引用，避免顶层 import 模型
    """取某任务最近 ``limit`` 条终端日志，按时间（id）升序返回。"""
    from app.models.voyage import VoyageTerminalLog

    # 取最近 limit 条（id 降序）再翻正，保证长任务只回放尾部且顺序正确。
    rows = (
        (
            await session.execute(
                select(VoyageTerminalLog)
                .where(VoyageTerminalLog.run_id == run_id)
                .order_by(VoyageTerminalLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))
