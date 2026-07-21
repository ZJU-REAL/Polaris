"""实时事件总线：向 Redis 频道发布 JSON 事件（频道约定见 docs/api-m1.md §6）。

- ``voyage:{id}:events``：voyage 引擎发布，SSE 端点订阅转发
- ``notify:project:{project_id}``：gate/voyage 状态变化，WS 端点订阅转发
- ``crdt:stream``：worker（AI 起草）发布分节流式增量，API 进程订阅后代写活跃
  CRDT 房间（跨进程直播 AI 撰写；worker 与 API 是独立容器，房间只在 API 进程）
"""

import json
import uuid
from typing import Any

from redis.asyncio import Redis

# AI 起草流式镜像频道（单频道，payload 内含 file_id）
CRDT_STREAM_CHANNEL = "crdt:stream"


def voyage_channel(voyage_id: uuid.UUID | str) -> str:
    return f"voyage:{voyage_id}:events"


def notify_channel(project_id: uuid.UUID | str) -> str:
    return f"notify:project:{project_id}"


class EventBus:
    """薄封装：把事件序列化为 JSON 发布到对应频道。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish_voyage_event(
        self, voyage_id: uuid.UUID | str, event: str, data: dict[str, Any]
    ) -> None:
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        await self._redis.publish(voyage_channel(voyage_id), payload)

    async def publish_notify(self, project_id: uuid.UUID | str, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, default=str)
        await self._redis.publish(notify_channel(project_id), payload)

    async def publish_crdt_stream(self, command: dict[str, Any]) -> None:
        """AI 起草分节流式命令（op=open|delta|replace，含 file_id/section/text）。"""
        payload = json.dumps(command, ensure_ascii=False, default=str)
        await self._redis.publish(CRDT_STREAM_CHANNEL, payload)


async def get_event_bus() -> EventBus:
    """FastAPI 依赖；测试覆盖为记录器或 fakeredis 总线。"""
    from app.core.redis import get_redis

    return EventBus(get_redis())
