"""WebSocket 通知（docs/api-m1.md §5）。

``WS /ws/notifications?token=<jwt>``：手动校验 JWT（复用 fastapi-users
JWTStrategy.read_token），订阅用户所在全部项目的 ``notify:project:{id}``
频道并转发（gate.created / gate.decided / voyage.status）。
"""

import asyncio
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.auth import UserManager, get_jwt_strategy
from app.core.db import get_sessionmaker
from app.core.events import notify_channel
from app.core.redis import get_redis
from app.models.project import ProjectMember
from app.models.user import User

router = APIRouter()


async def authenticate_ws_token(token: str | None) -> User | None:
    """校验 query 传入的 JWT，返回激活用户（失败 None）。"""
    if not token:
        return None
    strategy = get_jwt_strategy()
    async with get_sessionmaker()() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        manager = UserManager(user_db)
        user = await strategy.read_token(token, manager)
    if user is None or not user.is_active:
        return None
    return user


async def _user_project_ids(user_id: uuid.UUID) -> list[uuid.UUID]:
    async with get_sessionmaker()() as session:
        stmt = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        return [pid for (pid,) in (await session.execute(stmt)).all()]


@router.websocket("/ws/notifications")
async def notifications_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    user = await authenticate_ws_token(token)
    if user is None:
        # 握手后立即关闭：4401 = 未认证（policy violation 区间自定义码）
        await websocket.close(code=4401)
        return
    await websocket.accept()

    project_ids = await _user_project_ids(user.id)
    redis: Redis = get_redis()
    pubsub = redis.pubsub()
    channels = [notify_channel(pid) for pid in project_ids]
    if channels:
        await pubsub.subscribe(*channels)

    async def forward() -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
            if message is None:
                continue
            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            await websocket.send_text(raw)

    async def watch_disconnect() -> None:
        # 客户端无需发消息；receive 用于感知断开（ping/pong 由协议层处理）
        while True:
            await websocket.receive_text()

    tasks = [asyncio.create_task(forward()), asyncio.create_task(watch_disconnect())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        if channels:
            await pubsub.unsubscribe(*channels)
        await pubsub.aclose()
