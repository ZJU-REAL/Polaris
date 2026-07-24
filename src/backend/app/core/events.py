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


def paper_task_channel(task_id: str) -> str:
    """手动添加文献的分阶段处理进度频道（SSE 端点订阅转发）。"""
    return f"paper_task:{task_id}:events"


def paper_task_log_key(task_id: str) -> str:
    """进度事件的有序回放日志（Redis list）。

    pub/sub 不给迟到订阅者补发历史：后台任务往往在前端 SSE 连上之前就发完事件
    （dev 下无网络/LLM 时几毫秒跑完），只订频道会永远收不到、卡在处理中。故每条
    事件同时 append 到此 list（带 TTL），SSE 端点连上先回放它再接实时流。
    """
    return f"paper_task:{task_id}:log"


_PAPER_TASK_LOG_TTL = 600  # 回放日志存活时间（秒），与归属 key 一致


async def publish_paper_task_event(
    bus: "EventBus", task_id: str, event: str, data: dict[str, Any]
) -> None:
    """发布一条论文处理进度事件：先落回放日志（list + TTL），再发频道。

    与 voyage 事件不同：没有 voyage 行，不落库；但进度事件有竞态（任务可能早于
    订阅者发完），故用一个短存活的 Redis list 做有序回放，SSE 连上时先补历史。
    """
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
    log_key = paper_task_log_key(task_id)
    # 先 append 再 publish：迟到订阅者能从 list 回放；已订阅者从频道实时拿。
    pipe = bus._redis.pipeline()
    pipe.rpush(log_key, payload)
    pipe.expire(log_key, _PAPER_TASK_LOG_TTL)
    await pipe.execute()
    await bus._redis.publish(paper_task_channel(task_id), payload)


class EventBus:
    """薄封装：把事件序列化为 JSON 发布到对应频道。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish_voyage_event(
        self, voyage_id: uuid.UUID | str, event: str, data: dict[str, Any]
    ) -> None:
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        await self._redis.publish(voyage_channel(voyage_id), payload)
        # 结构化日志行落库，供刷新后 / 事后回看（大模型完整输出在 llm_end 处单独落库）。
        if event == "log" and data.get("message"):
            from app.services.voyage_logs import record_terminal_log

            await record_terminal_log(
                voyage_id,
                "log",
                message=str(data["message"]),
                level=data.get("level"),
            )

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
