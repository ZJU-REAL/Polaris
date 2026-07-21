"""AI 起草流式镜像订阅器（仅 API 进程，随 lifespan 启停）。

worker（独立容器）撰写论文时把分节增量发布到 redis ``crdt:stream`` 频道；
API 进程订阅后应用到活跃 CRDT 房间，让连接中的编辑器实时看到 AI「打字」。
worker 的权威 DB 写与版本快照仍在 worker 侧独立完成，本订阅器只管直播镜像，
连不上/无订阅时静默降级（不影响起草本身）。
"""

import asyncio
import contextlib
import json
import logging
import uuid

from app.core.events import CRDT_STREAM_CHANNEL
from app.core.redis import get_redis
from app.services.crdt_rooms import get_crdt_rooms

logger = logging.getLogger("polaris.crdt")


class CRDTStreamSubscriber:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    async def _run(self) -> None:
        pubsub = get_redis().pubsub()
        try:
            await pubsub.subscribe(CRDT_STREAM_CHANNEL)
        except Exception:  # noqa: BLE001 — redis 不可用：放弃直播，不阻塞服务
            logger.warning("CRDT 流式镜像订阅启动失败（redis 不可用？）", exc_info=True)
            return
        rooms = get_crdt_rooms()
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if msg is None:
                    continue
                await self._dispatch(rooms, msg.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 订阅循环异常不应拖垮进程
            logger.exception("CRDT 流式镜像订阅循环异常")
        finally:
            try:
                await pubsub.unsubscribe(CRDT_STREAM_CHANNEL)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _dispatch(rooms, raw) -> None:
        if raw is None:
            return
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            cmd = json.loads(raw)
            file_id = uuid.UUID(str(cmd["file_id"]))
            await rooms.stream_section(
                file_id, str(cmd["section"]), op=str(cmd["op"]), text=cmd.get("text", "")
            )
        except Exception:  # noqa: BLE001 — 单条坏消息不影响后续
            logger.warning("CRDT 流式镜像消息处理失败", exc_info=True)


_subscriber: CRDTStreamSubscriber | None = None


def get_crdt_stream_subscriber() -> CRDTStreamSubscriber:
    global _subscriber
    if _subscriber is None:
        _subscriber = CRDTStreamSubscriber()
    return _subscriber


async def stop_crdt_stream_subscriber() -> None:
    global _subscriber
    if _subscriber is not None:
        await _subscriber.stop()
    _subscriber = None
