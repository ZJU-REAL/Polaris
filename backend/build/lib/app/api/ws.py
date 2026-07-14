"""WebSocket 端点。

- ``WS /ws/notifications?token=<jwt>``（docs/api-m1.md §5）：手动校验 JWT（复用
  fastapi-users JWTStrategy.read_token），订阅用户所在全部项目的
  ``notify:project:{id}`` 频道并转发（gate.created / gate.decided / voyage.status /
  manuscript.status）；
- ``WS /ws/manuscripts/{file_id}?token=<jwt>``（docs/api-m5-b.md §6）：pycrdt CRDT
  协同房间（y-websocket 二进制协议，房间名 = file id）。on_connect 校验
  JWT + 项目成员 + 文件存在且非 readonly，失败分别以 4401 / 4404 / 4403 关闭。
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
from app.services import manuscripts as manuscripts_service
from app.services.crdt_rooms import get_crdt_rooms

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


class _CRDTChannel:
    """FastAPI WebSocket → pycrdt.websocket Channel 协议桥（二进制消息，path=房间名）。"""

    def __init__(self, websocket: WebSocket, path: str) -> None:
        self._websocket = websocket
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self) -> "_CRDTChannel":
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._websocket.receive_bytes()
        except WebSocketDisconnect as e:
            raise StopAsyncIteration from e

    async def send(self, message: bytes) -> None:
        await self._websocket.send_bytes(bytes(message))

    async def recv(self) -> bytes:
        return await self._websocket.receive_bytes()


@router.websocket("/ws/manuscripts/{file_id}")
async def manuscript_crdt_ws(
    websocket: WebSocket,
    file_id: uuid.UUID,
    token: str | None = Query(default=None),
) -> None:
    user = await authenticate_ws_token(token)
    if user is None:
        await websocket.close(code=4401)  # 未认证
        return
    async with get_sessionmaker()() as session:
        file = await manuscripts_service.get_file_for_user(
            session, file_id=file_id, user_id=user.id
        )
    if file is None:
        await websocket.close(code=4404)  # 不存在或非项目成员（不泄露存在性）
        return
    if file.readonly:
        await websocket.close(code=4403)  # 只读模板文件不开协同房间
        return

    await websocket.accept()
    rooms = get_crdt_rooms()
    await rooms.connect(file_id=file.id, db_content=file.content)
    try:
        await rooms.serve(_CRDTChannel(websocket, str(file.id)))
    except WebSocketDisconnect:
        pass
    finally:
        # 断开即冲刷快照（防抖窗口内的最后编辑不丢）
        await rooms.flush(file.id)
