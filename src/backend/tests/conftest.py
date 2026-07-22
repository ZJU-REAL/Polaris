"""测试夹具：临时 sqlite + httpx AsyncClient(ASGITransport)。

注意：必须在 import app 之前设置环境变量（Settings 是 lru_cache 的）。
"""

import os
import re
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
os.environ["POLARIS_LLM_FAKE_FALLBACK"] = "1"  # 测试套件依赖确定性 fake provider

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
        self.crdt_stream: list[dict] = []

    async def publish_voyage_event(self, voyage_id, event: str, data: dict) -> None:
        self.voyage_events.append((str(voyage_id), event, data))

    async def publish_notify(self, project_id, message: dict) -> None:
        self.notify.append((str(project_id), message))

    async def publish_crdt_stream(self, command: dict) -> None:
        self.crdt_stream.append(command)


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

# ---- P4 内容池造数据助手 ----

# 判断字段落 library_papers 成员行（其余入 Paper 内容池行）
_MEMBERSHIP_FIELDS = (
    "relevance_score",
    "wiki_content",
    "status",
    "trash_reason",
    "scored_at",
    "compiled_at",
    "compiled_model",
)


async def add_paper(session, *, project_id, **fields):
    """测试造数据统一入口：建内容池 Paper + 隐式库成员行，返回 Paper。

    兼容旧单表口径：status/relevance_score/wiki_content 等判断字段自动落成员行。
    """
    import uuid as _uuid

    from app.models.library_direction import LibraryPaper
    from app.models.paper import Paper
    from app.services.libraries import get_library_for_project

    member_kwargs = {k: fields.pop(k) for k in list(fields) if k in _MEMBERSHIP_FIELDS}
    member_kwargs.setdefault("status", "candidate")
    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    paper = Paper(**fields)
    session.add(paper)
    await session.flush()
    session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, **member_kwargs))
    await session.flush()
    return paper


async def membership_of(session, *, project_id, paper_id):
    """取论文在某方向隐式库的成员行（断言判断字段用）。"""
    import uuid as _uuid

    from app.services.libraries import get_library_for_project, get_membership

    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    return await get_membership(
        session,
        library_id=library.id,
        paper_id=paper_id if isinstance(paper_id, _uuid.UUID) else _uuid.UUID(str(paper_id)),
    )


async def project_paper_rows(session, *, project_id):
    """某方向隐式库的 (Paper, LibraryPaper) 全量列表（测试断言判断字段用）。"""
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.library_direction import LibraryPaper
    from app.models.paper import Paper
    from app.services.libraries import get_library_for_project

    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    rows = (
        await session.execute(
            select(Paper, LibraryPaper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(LibraryPaper.library_id == library.id)
        )
    ).all()
    return [(paper, membership) for paper, membership in rows]


async def project_concepts(session, *, project_id):
    """某方向隐式库的概念列表。"""
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.paper import Concept
    from app.services.libraries import get_library_for_project

    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    return list(
        (
            await session.execute(select(Concept).where(Concept.library_id == library.id))
        ).scalars()
    )


async def add_concept(session, *, project_id, **fields):
    """建方向库概念（旧 Concept(project_id=...) 口径的替代）。"""
    import uuid as _uuid

    from app.models.paper import Concept
    from app.services.libraries import get_library_for_project

    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    concept = Concept(library_id=library.id, **fields)
    session.add(concept)
    await session.flush()
    return concept


def _username_from_email(email: str) -> str:
    """从 email 派生一个合法用户名（小写字母/数字/下划线 3-32 位）。"""
    local = email.split("@", 1)[0].lower()
    uname = re.sub(r"[^a-z0-9_]", "_", local)
    return (uname + "_u")[:32] if len(uname) < 3 else uname[:32]


async def register_and_login(
    client: AsyncClient, email: str = "alice@example.com", username: str | None = None
) -> str:
    """注册 + 登录，返回 Bearer token。"""
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "str0ng-password",
            "display_name": "Alice",
            "username": username or _username_from_email(email),
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
