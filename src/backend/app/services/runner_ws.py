"""BYO runner tier-2（#695，设计草案 docs/byo-runner.md）：出站 WebSocket 传输层。

场景：NAT/内网后的机器平台连不进去（tier-1 的 SSH 直连走不通）。学 GitHub
self-hosted runner：机器上跑常驻 agent（integrations/runner-agent/），**出站**
长连接回平台拉任务，永不要求开入站端口。

本模块只做「传输层」四件事，**不接实验执行链**（runner v2 在 ws 底座上的
接线归后续 PR，见 dispatch_task 的注释留位）：

1. **注册 token**：短时效一次性 token（随机串 + Redis TTL + GETDEL 原子消费）。
   选 Redis 而非 Fernet 自包含 token：一次性语义必须有服务端状态才能保证
   （Fernet 只能保证过期，防重放还得回 Redis），不如直接一个 key 又短又可靠。
2. **agent 注册**：token 换长期机器凭据（agent secret）。secret 只存 sha256
   摘要（Fernet 加密后落 connection_credentials.payload_encrypted，kind='ws'）
   ——服务端永远不需要回读明文，摘要即可验证，泄库也拼不回 secret。
   同时建 host 类 Resource（config.transport="websocket"），与 tier-1 同一张表，
   租约互斥/容量语义天然共用（#680）。
3. **WS 服务循环**：首消息鉴权（accept 之后才能 receive，故先 accept 再验，
   失败 4401 关闭）；在线状态双写——进程内 hub 计数（同资源允许多连接，
   派发经 Redis lpop 天然只投一份）+ Resource.config.agent_online（跨进程/
   前端可见，只在连接建立与断开时写库，心跳不写）。心跳超时 4408 判离线。
4. **派发座**：dispatch_task 把任务压进 Redis list（离线排队，TTL 兜底）并
   publish 唤醒；连接侧「先订阅唤醒频道、再排空队列、每次唤醒/轮询再排空」
   ——在线即推、离线排队、重连补投三种情形同一条路径覆盖，且 worker 进程
   （无 WS 连接）也能经 Redis 向 API 进程的连接派发。

事件回传选「专用 Redis 记录」而非 paper-task 事件通道：paper_task:* 是文献
处理进度的约定（task_id 语义不同、消费方是文献 SSE），混用会把两个领域的
key 空间搅在一起。这里按同样的「回放 list + 实时频道」模式另起 runner:task:*
前缀，runner v2 接线时既可回放也可实时订阅。

帧协议（JSON 文本帧，docs/byo-runner.md tier-2 节）：
- agent → server（首帧）：{"type": "auth", "resource_id": …, "secret": …}
- server → agent：{"type": "ready"} / {"type": "task", "task_id": …, "payload": …}
- agent → server：{"type": "heartbeat"} / {"type": "event"|"result",
  "task_id": …, "data": …}
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.resource import Resource
from app.models.ssh_credential import ConnectionCredential

logger = logging.getLogger("polaris.runner_ws")

# ---------------------------------------------------------------------------
# 常量与 Redis key 约定
# ---------------------------------------------------------------------------

REGISTRATION_TOKEN_TTL = 3600  # 注册 token 有效期（秒）；一次性，用后即焚
TASK_QUEUE_TTL = 3600  # 离线任务排队的兜底 TTL：机器长期不上线时任务自然过期
TASK_EVENTS_TTL = 3600  # 事件回放 list 的存活期（同 paper_task 日志口径）
HEARTBEAT_TIMEOUT = 90.0  # 秒内收不到任何帧（含心跳）即判离线
AUTH_TIMEOUT = 10.0  # 连上后首帧鉴权的等待上限
WAKE_POLL_TIMEOUT = 5.0  # 唤醒频道的轮询间隔（错过 publish 时的兜底补扫）

# 自定义关闭码（4000+ 区间；4401 与现有 WS 端点的「未认证」口径一致）
CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403
CLOSE_HEARTBEAT_TIMEOUT = 4408


def registration_token_key(token: str) -> str:
    return f"runner:regtoken:{token}"


def task_queue_key(resource_id: uuid.UUID | str) -> str:
    return f"runner:{resource_id}:tasks"


def wake_channel(resource_id: uuid.UUID | str) -> str:
    return f"runner:{resource_id}:wake"


def task_events_key(task_id: str) -> str:
    return f"runner:task:{task_id}:events"


def task_events_channel(task_id: str) -> str:
    return f"runner:task:{task_id}:events:live"


# ---------------------------------------------------------------------------
# 注册 token（一次性、短时效）
# ---------------------------------------------------------------------------


async def issue_registration_token(
    redis: Redis, *, user_id: uuid.UUID, ttl: int = REGISTRATION_TOKEN_TTL
) -> str:
    """签发注册 token：随机串落 Redis（值 = 签发者 user_id），到期自动蒸发。"""
    token = "prt_" + secrets.token_urlsafe(24)
    await redis.set(registration_token_key(token), str(user_id), ex=ttl)
    return token


async def consume_registration_token(redis: Redis, token: str) -> uuid.UUID | None:
    """消费注册 token：GETDEL 原子取走（并发重放只有一个赢家），无效/过期返回 None。"""
    if not token or not token.startswith("prt_"):
        return None
    value = await redis.getdel(registration_token_key(token))
    if not value:
        return None
    try:
        return uuid.UUID(value if isinstance(value, str) else value.decode())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# agent 注册：token → host Resource + 机器凭据（agent secret）
# ---------------------------------------------------------------------------


async def register_agent_host(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    name: str,
    machine: dict[str, Any] | None = None,
) -> tuple[Resource, str]:
    """建 host 类 Resource + kind='ws' 机器凭据，返回 (资源, agent secret)。

    - secret 只在这里生成并返回一次，库里仅存 sha256 摘要（再 Fernet 加密）；
    - config.ephemeral 恒为 True 且不提供关闭口：tier-2 的任务一律容器内执行，
      tier-1 的裸机 non-ephemeral 路径不下放（GitHub runner 残留物事故的教训）；
    - 与 tier-1 注册同构（都是 host Resource），租约互斥天然生效。
    """
    agent_secret = "pra_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(agent_secret.encode("utf-8")).hexdigest()
    hostname = str((machine or {}).get("hostname") or "outbound")[:255]
    credential = ConnectionCredential(
        user_id=owner_id,
        kind="ws",
        name=f"runner-agent:{name}"[:255],
        # host/port 仅作展示：连接方向是 agent 出站，平台从不主动连它
        host=hostname,
        port=443,
        payload_encrypted=encrypt_secret(json.dumps({"secret_sha256": digest})),
    )
    session.add(credential)
    await session.flush()
    config: dict[str, Any] = {
        "transport": "websocket",
        "ephemeral": True,
        "agent_online": False,
    }
    if machine:
        config["machine"] = machine
    resource = Resource(
        owner_id=owner_id,
        name=name,
        kind="host",
        capacity=1,
        exclusive=True,
        credential_id=credential.id,
        config=config,
    )
    session.add(resource)
    await session.commit()
    await session.refresh(resource)
    return resource, agent_secret


async def _verify_agent_secret(resource_id: uuid.UUID, secret: str) -> Resource | None:
    """按 resource_id 定位凭据并校验 secret 摘要（常数时间比较）。

    资源必须是 websocket 传输的 host、未被吊销（config.unavailable）；凭据必须
    kind='ws'。任何一环不满足都返回 None——鉴权失败不区分原因，不泄露存在性。
    """
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        if resource is None or resource.kind != "host":
            return None
        config = resource.config or {}
        if config.get("transport") != "websocket" or config.get("unavailable"):
            return None
        if resource.credential_id is None:
            return None
        credential = await session.get(ConnectionCredential, resource.credential_id)
        if credential is None or credential.kind != "ws" or not credential.payload_encrypted:
            return None
        try:
            payload = json.loads(decrypt_secret(credential.payload_encrypted))
        except Exception:  # 解密失败按无效凭据处理（换过加密密钥等）
            return None
        stored = str(payload.get("secret_sha256") or "")
        given = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not stored or not hmac.compare_digest(stored, given):
            return None
        return resource


# ---------------------------------------------------------------------------
# 在线登记（进程内 hub + Resource.config 双写）
# ---------------------------------------------------------------------------


class RunnerHub:
    """进程内连接登记：resource_id → 活跃连接数。

    允许同一资源多连接（agent 重启后旧连接可能要等心跳超时才消失，若拒绝新连
    会把重连挡在门外一整个超时窗口）；任务经 Redis lpop 派发，多连接也只投一份。
    只有计数归零才写库标离线。
    """

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, int] = {}

    def connect(self, resource_id: uuid.UUID) -> None:
        self._connections[resource_id] = self._connections.get(resource_id, 0) + 1

    def disconnect(self, resource_id: uuid.UUID) -> bool:
        """减计数；返回是否已无任何连接（需要写库标离线）。"""
        count = self._connections.get(resource_id, 0) - 1
        if count <= 0:
            self._connections.pop(resource_id, None)
            return True
        self._connections[resource_id] = count
        return False

    def is_online(self, resource_id: uuid.UUID) -> bool:
        return self._connections.get(resource_id, 0) > 0


_hub = RunnerHub()


def get_runner_hub() -> RunnerHub:
    return _hub


async def _mark_agent_presence(resource_id: uuid.UUID, *, online: bool) -> None:
    """把在线状态落到 Resource.config（跨进程/前端可见）。只在连接边沿写。"""
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        if resource is None:  # 连接期间资源被删：无处可写，直接返回
            return
        merged = dict(resource.config or {})
        merged["agent_online"] = online
        merged["agent_last_seen"] = utcnow().isoformat()
        resource.config = merged
        await session.commit()


async def _resource_still_valid(resource_id: uuid.UUID) -> bool:
    """心跳时复核资源仍可用：凭据吊销（credential_id 清空 + unavailable）后，
    活跃连接在下一次心跳被踢掉——不用额外的跨进程踢人通道，最迟一个心跳周期生效。"""
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        if resource is None or resource.credential_id is None:
            return False
        return not (resource.config or {}).get("unavailable")


# ---------------------------------------------------------------------------
# 派发座（transport-level dispatch seat）
# ---------------------------------------------------------------------------


async def dispatch_task(
    redis: Redis, resource_id: uuid.UUID | str, payload: dict[str, Any]
) -> str:
    """向某台 runner 机器派发一个任务，返回 task_id。

    统一路径：先压 Redis 队列（离线排队 + TTL 兜底），再 publish 唤醒在线连接。
    agent 在线 → 连接侧被唤醒立即 lpop 推送；离线 → 任务躺在队列里等重连补投。

    NOTE(runner-v2): 实验执行链接线时，payload 放流程包 + 物料 + container spec
    （docs/byo-runner.md tier-2 节），并由租约（resource_leases）决定派发对象；
    本 PR 只提供传输语义，payload 对传输层不透明。
    """
    task_id = uuid.uuid4().hex
    entry = json.dumps({"task_id": task_id, "payload": payload}, ensure_ascii=False)
    key = task_queue_key(resource_id)
    pipe = redis.pipeline()
    pipe.rpush(key, entry)
    pipe.expire(key, TASK_QUEUE_TTL)
    await pipe.execute()
    await redis.publish(wake_channel(resource_id), task_id)
    return task_id


async def record_agent_event(redis: Redis, task_id: str, frame: dict[str, Any]) -> None:
    """落一条 agent 回传（event/result）：回放 list + 实时频道，同 paper_task 模式。

    先 append 再 publish：迟到的消费方从 list 回放，已订阅的从频道实时拿。
    """
    payload = json.dumps(frame, ensure_ascii=False, default=str)
    key = task_events_key(task_id)
    pipe = redis.pipeline()
    pipe.rpush(key, payload)
    pipe.expire(key, TASK_EVENTS_TTL)
    await pipe.execute()
    await redis.publish(task_events_channel(task_id), payload)


async def fetch_task_events(redis: Redis, task_id: str) -> list[dict[str, Any]]:
    """回放某任务的全部回传帧（runner v2 接线前主要供测试与排查用）。"""
    raw_items = await redis.lrange(task_events_key(task_id), 0, -1)
    events: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            events.append(json.loads(raw))
        except ValueError:
            continue
    return events


# ---------------------------------------------------------------------------
# WS 服务循环
# ---------------------------------------------------------------------------


async def _authenticate_first_frame(
    websocket: WebSocket, auth_timeout: float
) -> Resource | None:
    """读首帧并鉴权。任何失败（超时/坏 JSON/断开/校验不过）都返回 None。"""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=auth_timeout)
        frame = json.loads(raw)
        if not isinstance(frame, dict) or frame.get("type") != "auth":
            return None
        resource_id = uuid.UUID(str(frame.get("resource_id")))
        secret = str(frame.get("secret") or "")
    except (TimeoutError, ValueError, WebSocketDisconnect):
        return None
    if not secret:
        return None
    return await _verify_agent_secret(resource_id, secret)


async def _drain_tasks(redis: Redis, websocket: WebSocket, resource_id: uuid.UUID) -> None:
    """排空该资源的待派发队列，逐条以 task 帧推给 agent。"""
    key = task_queue_key(resource_id)
    while True:
        raw = await redis.lpop(key)
        if raw is None:
            return
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # 队列里出现坏行只可能是外部手写，跳过别炸连接
        await websocket.send_text(
            json.dumps(
                {"type": "task", "task_id": entry.get("task_id"), "payload": entry.get("payload")},
                ensure_ascii=False,
            )
        )


async def _deliver_loop(
    redis: Redis, websocket: WebSocket, resource_id: uuid.UUID, wake_poll: float
) -> None:
    """派发侧：订阅唤醒频道 → 排空队列 → 每次唤醒（或轮询兜底）再排空。

    先订阅再排空：反过来会在「排空完、订阅前」漏掉一次 publish。轮询兜底
    （get_message 超时后也排空一次）让极端情况下的漏唤醒最迟 wake_poll 秒补上。
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(wake_channel(resource_id))
    try:
        while True:
            await _drain_tasks(redis, websocket, resource_id)
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=wake_poll)
    finally:
        await pubsub.unsubscribe(wake_channel(resource_id))
        await pubsub.aclose()


async def _receive_loop(
    redis: Redis, websocket: WebSocket, resource_id: uuid.UUID, heartbeat_timeout: float
) -> str:
    """接收侧：心跳/事件/结果。返回结束原因（disconnect|timeout|revoked）。"""
    while True:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=heartbeat_timeout)
        except TimeoutError:
            return "timeout"
        except WebSocketDisconnect:
            return "disconnect"
        try:
            frame = json.loads(raw)
        except ValueError:
            logger.warning("runner_ws.bad_frame resource=%s", resource_id)
            continue
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        if kind == "heartbeat":
            # 心跳顺带复核吊销：凭据被吊销的连接最迟一个心跳周期内被断开
            if not await _resource_still_valid(resource_id):
                return "revoked"
            continue
        if kind in ("event", "result"):
            task_id = frame.get("task_id")
            if task_id:
                await record_agent_event(redis, str(task_id), frame)
            continue
        logger.debug("runner_ws.unknown_frame resource=%s type=%r", resource_id, kind)


async def serve_agent(
    websocket: WebSocket,
    *,
    redis: Redis,
    hub: RunnerHub | None = None,
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
    auth_timeout: float = AUTH_TIMEOUT,
    wake_poll: float = WAKE_POLL_TIMEOUT,
) -> None:
    """一条 agent 连接的完整生命周期（WS 端点薄转发到这里，参数可注入便于测试）。"""
    hub = hub or _hub
    await websocket.accept()
    resource = await _authenticate_first_frame(websocket, auth_timeout)
    if resource is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    resource_id = resource.id

    hub.connect(resource_id)
    await _mark_agent_presence(resource_id, online=True)
    logger.info("runner_ws.connected resource=%s", resource_id)
    reason = "disconnect"
    try:
        await websocket.send_text(
            json.dumps({"type": "ready", "resource_id": str(resource_id)})
        )
        receiver = asyncio.create_task(
            _receive_loop(redis, websocket, resource_id, heartbeat_timeout)
        )
        deliverer = asyncio.create_task(_deliver_loop(redis, websocket, resource_id, wake_poll))
        try:
            done, pending = await asyncio.wait(
                {receiver, deliverer}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None:
                    if isinstance(exc, WebSocketDisconnect):
                        continue
                    raise exc
                if task is receiver:
                    reason = task.result()
        finally:
            for task in (receiver, deliverer):
                task.cancel()
            # 等取消真正落地：不等的话事件循环收尾时会冒 pending task 警告，
            # deliverer 的 pubsub 清理也没机会跑完
            await asyncio.gather(receiver, deliverer, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        if hub.disconnect(resource_id):
            await _mark_agent_presence(resource_id, online=False)
        logger.info("runner_ws.disconnected resource=%s reason=%s", resource_id, reason)
        close_code = {
            "timeout": CLOSE_HEARTBEAT_TIMEOUT,
            "revoked": CLOSE_REVOKED,
        }.get(reason)
        if close_code is not None:
            # 对端可能已断：重复 close 抛 RuntimeError，不追究
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=close_code)
