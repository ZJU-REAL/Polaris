"""BYO runner tier-2（#695）：注册 token / agent 注册 / WS 鉴权与派发 / 心跳与吊销。

WS 侧不走 httpx（AsyncClient 不支持 websocket），直接驱动 service 层的
serve_agent：注入假 WebSocket + fakeredis，超时参数调小让离线判定秒级可测。
"""

import asyncio
import hashlib
import json
import uuid

from starlette.websockets import WebSocketDisconnect

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret
from app.models.resource import Resource
from app.models.ssh_credential import ConnectionCredential
from app.services import runner_ws
from tests.conftest import register_and_login


async def _auth(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    return {"Authorization": f"Bearer {token}"}


async def _issue_token(client, headers) -> str:
    resp = await client.post("/api/resources/runner-hosts/registration-tokens", headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expires_in"] == runner_ws.REGISTRATION_TOKEN_TTL
    return body["token"]


async def _register(client, token, name="nat-box", machine=None):
    return await client.post(
        "/api/resources/runner-hosts/register",
        json={"token": token, "name": name, "machine": machine},
    )


async def _register_agent(client, fake_redis, name="nat-box"):
    """完整注册一台 tier-2 机器，返回 (resource_id, agent_secret, 用户 headers)。"""
    headers = await _auth(client)
    token = await _issue_token(client, headers)
    resp = await _register(client, token, name=name)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return uuid.UUID(body["resource"]["id"]), body["agent_secret"], headers


class FakeAgentSocket:
    """进程内假 agent 连接：喂帧给 serve_agent、收集它发出的帧。"""

    def __init__(self):
        self.accepted = False
        self.close_code: int | None = None
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise WebSocketDisconnect(code=1000)
        return item

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.close_code is None:
            self.close_code = code

    # ---- 测试侧助手 ----

    async def push(self, frame: dict) -> None:
        await self._incoming.put(json.dumps(frame))

    def drop(self) -> None:
        """模拟 agent 断线（receive 抛 WebSocketDisconnect）。"""
        self._incoming.put_nowait(None)

    async def wait_frame(self, ftype: str, timeout: float = 3.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for frame in self.sent:
                if frame.get("type") == ftype:
                    return frame
            assert asyncio.get_event_loop().time() < deadline, (
                f"no {ftype!r} frame within {timeout}s; got {self.sent}"
            )
            await asyncio.sleep(0.01)


def _serve(ws, fake_redis, hub, **overrides):
    kwargs = {"heartbeat_timeout": 5.0, "auth_timeout": 2.0, "wake_poll": 0.05}
    kwargs.update(overrides)
    return asyncio.create_task(
        runner_ws.serve_agent(ws, redis=fake_redis, hub=hub, **kwargs)
    )


async def _agent_online(resource_id: uuid.UUID) -> bool:
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        return bool((resource.config or {}).get("agent_online"))


# ---- 注册 token：一次性、短时效 ----


async def test_registration_token_requires_login(client, fake_redis):
    resp = await client.post("/api/resources/runner-hosts/registration-tokens")
    assert resp.status_code == 401


async def test_register_flow_and_token_single_use(client, fake_redis):
    headers = await _auth(client)
    token = await _issue_token(client, headers)
    assert token.startswith("prt_")
    # 短时效：key 带 TTL（到期自动蒸发）
    ttl = await fake_redis.ttl(runner_ws.registration_token_key(token))
    assert 0 < ttl <= runner_ws.REGISTRATION_TOKEN_TTL

    machine = {"hostname": "gpu-home", "os": "Linux", "cpu_count": 16}
    resp = await _register(client, token, name="home-box", machine=machine)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_secret"].startswith("pra_")
    assert body["ws_path"] == "/ws/runner-agents/connect"
    resource = body["resource"]
    assert resource["kind"] == "host" and resource["exclusive"] is True
    assert resource["config"]["transport"] == "websocket"
    assert resource["config"]["ephemeral"] is True  # tier-2 恒 ephemeral，无关闭口
    assert resource["config"]["agent_online"] is False
    assert resource["config"]["machine"] == machine

    # 机器凭据：kind=ws，只存 secret 摘要（加密），明文任何列都不落
    async with get_sessionmaker()() as session:
        credential = await session.get(
            ConnectionCredential, uuid.UUID(resource["credential_id"])
        )
        assert credential.kind == "ws"
        assert credential.host == "gpu-home"
        assert credential.private_key_encrypted is None
        payload = json.loads(decrypt_secret(credential.payload_encrypted))
        expected = hashlib.sha256(body["agent_secret"].encode()).hexdigest()
        assert payload == {"secret_sha256": expected}

    # 一次性：token 用后即焚，重放 401
    resp = await _register(client, token)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "INVALID_REGISTRATION_TOKEN"
    # 凭空编的 token 同样 401
    resp = await _register(client, "prt_forged")
    assert resp.status_code == 401


async def test_registration_token_expiry(client, fake_redis):
    headers = await _auth(client)
    token = await _issue_token(client, headers)
    # 模拟到期：key 蒸发后 token 不再可用
    await fake_redis.delete(runner_ws.registration_token_key(token))
    resp = await _register(client, token)
    assert resp.status_code == 401


# ---- WS 鉴权 ----


async def test_ws_rejects_bad_secret(client, fake_redis):
    resource_id, _secret, _headers = await _register_agent(client, fake_redis)
    hub = runner_ws.RunnerHub()

    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub)
    await ws.push({"type": "auth", "resource_id": str(resource_id), "secret": "pra_wrong"})
    await asyncio.wait_for(task, timeout=3.0)
    assert ws.accepted and ws.close_code == runner_ws.CLOSE_UNAUTHORIZED
    assert not hub.is_online(resource_id)
    assert await _agent_online(resource_id) is False

    # 首帧不是 auth：同样 4401 关闭
    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub)
    await ws.push({"type": "heartbeat"})
    await asyncio.wait_for(task, timeout=3.0)
    assert ws.close_code == runner_ws.CLOSE_UNAUTHORIZED


# ---- 在线派发与事件回传 ----


async def test_online_dispatch_and_event_roundtrip(client, fake_redis):
    resource_id, secret, _headers = await _register_agent(client, fake_redis)
    hub = runner_ws.RunnerHub()
    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub)
    await ws.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    ready = await ws.wait_frame("ready")
    assert ready["resource_id"] == str(resource_id)
    assert hub.is_online(resource_id)
    assert await _agent_online(resource_id) is True

    # 在线派发：任务被立即推给 agent
    payload = {"kind": "echo", "msg": "hi"}
    task_id = await runner_ws.dispatch_task(fake_redis, resource_id, payload)
    frame = await ws.wait_frame("task")
    assert frame["task_id"] == task_id and frame["payload"] == payload

    # 事件回传：event + result 落专用记录，可回放
    await ws.push({"type": "event", "task_id": task_id, "data": {"status": "received"}})
    await ws.push({"type": "result", "task_id": task_id, "data": {"echo": payload}})
    deadline = asyncio.get_event_loop().time() + 3.0
    while True:
        events = await runner_ws.fetch_task_events(fake_redis, task_id)
        if len(events) >= 2:
            break
        assert asyncio.get_event_loop().time() < deadline, events
        await asyncio.sleep(0.01)
    assert events[0]["type"] == "event" and events[0]["data"] == {"status": "received"}
    assert events[1]["type"] == "result" and events[1]["data"] == {"echo": payload}

    # 断线：hub 与库里都标离线
    ws.drop()
    await asyncio.wait_for(task, timeout=3.0)
    assert not hub.is_online(resource_id)
    assert await _agent_online(resource_id) is False


async def test_offline_queue_and_reconnect_delivery(client, fake_redis):
    resource_id, secret, _headers = await _register_agent(client, fake_redis)

    # 离线派发：任务进队列（带 TTL 兜底），没人在线也不丢
    task_id = await runner_ws.dispatch_task(fake_redis, resource_id, {"kind": "echo"})
    queue_key = runner_ws.task_queue_key(resource_id)
    assert await fake_redis.llen(queue_key) == 1
    assert 0 < await fake_redis.ttl(queue_key) <= runner_ws.TASK_QUEUE_TTL

    # 重连补投：agent 一上线就收到排队中的任务，队列排空
    hub = runner_ws.RunnerHub()
    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub)
    await ws.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    frame = await ws.wait_frame("task")
    assert frame["task_id"] == task_id
    assert await fake_redis.llen(queue_key) == 0

    ws.drop()
    await asyncio.wait_for(task, timeout=3.0)


# ---- 心跳超时与凭据吊销 ----


async def test_heartbeat_timeout_marks_offline(client, fake_redis):
    resource_id, secret, _headers = await _register_agent(client, fake_redis)
    hub = runner_ws.RunnerHub()
    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub, heartbeat_timeout=0.15)
    await ws.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    await ws.wait_frame("ready")

    # 不发任何帧：心跳窗口过后服务端主动关闭并标离线
    await asyncio.wait_for(task, timeout=3.0)
    assert ws.close_code == runner_ws.CLOSE_HEARTBEAT_TIMEOUT
    assert not hub.is_online(resource_id)
    assert await _agent_online(resource_id) is False

    # 心跳按时到则保持在线（帧到达即重置接收窗口）
    ws2 = FakeAgentSocket()
    task2 = _serve(ws2, fake_redis, hub, heartbeat_timeout=0.5)
    await ws2.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    await ws2.wait_frame("ready")
    for _ in range(3):
        await asyncio.sleep(0.2)
        await ws2.push({"type": "heartbeat"})
    assert not task2.done()
    assert hub.is_online(resource_id)
    ws2.drop()
    await asyncio.wait_for(task2, timeout=3.0)


async def test_revoked_credential_kicks_connection(client, fake_redis):
    """吊销机器凭据后：新连接 4401 拒绝；在跳的连接下一次心跳被 4403 踢掉。"""
    resource_id, secret, headers = await _register_agent(client, fake_redis)
    hub = runner_ws.RunnerHub()
    ws = FakeAgentSocket()
    task = _serve(ws, fake_redis, hub)
    await ws.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    await ws.wait_frame("ready")

    # 吊销：走通用凭据删除端点（#685 的吊销语义对 kind=ws 同样生效）
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        credential_id = resource.credential_id
    resp = await client.delete(f"/api/connection-credentials/{credential_id}", headers=headers)
    assert resp.status_code == 204, resp.text

    await ws.push({"type": "heartbeat"})
    await asyncio.wait_for(task, timeout=3.0)
    assert ws.close_code == runner_ws.CLOSE_REVOKED
    assert not hub.is_online(resource_id)

    # 吊销后的重连被拒（资源 unavailable + 凭据已删）
    ws3 = FakeAgentSocket()
    task3 = _serve(ws3, fake_redis, hub)
    await ws3.push({"type": "auth", "resource_id": str(resource_id), "secret": secret})
    await asyncio.wait_for(task3, timeout=3.0)
    assert ws3.close_code == runner_ws.CLOSE_UNAUTHORIZED
