"""测试夹具：临时 sqlite + httpx AsyncClient(ASGITransport)。

注意：必须在 import app 之前设置环境变量（Settings 是 lru_cache 的）。
"""

import os
import tempfile

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TMPDIR = tempfile.mkdtemp(prefix="polaris-test-")
os.environ["POLARIS_ENV"] = "dev"
os.environ["POLARIS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR}/test.db"
os.environ["POLARIS_SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["POLARIS_INVITE_CODE"] = "test-invite"
os.environ["POLARIS_ENCRYPTION_KEY"] = ""
os.environ["POLARIS_DATA_DIR"] = f"{_TMPDIR}/data"  # PDF/全文落盘目录（M2）

from app.core.db import Base, dispose_engine, get_engine  # noqa: E402
from app.core.events import get_event_bus  # noqa: E402
from app.core.llm.router import reset_llm_router  # noqa: E402
from app.core.queue import get_task_queue  # noqa: E402
from app.core.redis import get_redis_dep  # noqa: E402
from app.main import create_app  # noqa: E402

import app.models  # noqa: E402,F401  isort: skip  注册全部表


@pytest_asyncio.fixture
async def app():
    """每个测试一套干净的表（ASGITransport 不跑 lifespan，手动建表）。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    reset_llm_router()  # 丢弃路由缓存，避免跨测试串味
    yield create_app()
    await dispose_engine()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class StubQueue:
    """记录 enqueue 调用的假任务队列（测试替代 ARQ/redis）。"""

    def __init__(self):
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue(self, func: str, *args, **kwargs) -> None:
        self.jobs.append((func, args, kwargs))


class RecordingBus:
    """记录事件发布的假 EventBus。"""

    def __init__(self):
        self.voyage_events: list[tuple[str, str, dict]] = []
        self.notify: list[tuple[str, dict]] = []

    async def publish_voyage_event(self, voyage_id, event: str, data: dict) -> None:
        self.voyage_events.append((str(voyage_id), event, data))

    async def publish_notify(self, project_id, message: dict) -> None:
        self.notify.append((str(project_id), message))


@pytest_asyncio.fixture
async def queue_stub(app):
    stub = StubQueue()
    app.dependency_overrides[get_task_queue] = lambda: stub
    return stub


@pytest_asyncio.fixture
async def bus_recorder(app):
    bus = RecordingBus()
    app.dependency_overrides[get_event_bus] = lambda: bus
    return bus


@pytest_asyncio.fixture
async def fake_redis(app):
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_redis_dep] = lambda: redis
    yield redis
    await redis.aclose()


INVITE_CODE = "test-invite"


async def register_and_login(client: AsyncClient, email: str = "alice@example.com") -> str:
    """注册 + 登录，返回 Bearer token。"""
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "str0ng-password",
            "display_name": "Alice",
            "invite_code": INVITE_CODE,
        },
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/auth/jwt/login",
        data={"username": email, "password": "str0ng-password"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]
