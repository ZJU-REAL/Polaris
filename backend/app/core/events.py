"""实时事件总线：向 Redis 频道发布 JSON 事件（频道约定见 docs/api-m1.md §6）。

- ``voyage:{id}:events``：voyage 引擎发布，SSE 端点订阅转发
- ``notify:project:{project_id}``：gate/voyage 状态变化，WS 端点订阅转发
"""

import json
import uuid
from typing import Any

from redis.asyncio import Redis


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


async def get_event_bus() -> EventBus:
    """FastAPI 依赖；测试覆盖为记录器或 fakeredis 总线。"""
    from app.core.redis import get_redis

    return EventBus(get_redis())
