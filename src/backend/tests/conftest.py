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
    "status",
    "trash_reason",
    "scored_at",
)
# 解读落 paper_wikis（每篇论文一份）：旧口径的 wiki_content / compiled_model 映射过去
_WIKI_FIELDS = {"wiki_content": "content", "compiled_model": "model"}


async def ensure_project_library(session, project_id):
    """确保课题有一个 active 起源库并已关联；没有则新建（测试造数据用）。

    P9c 起 create_project 不再自动建隐式库——语料相关测试经此显式补一个
    project_id 回指的 active 库并登记为关联，等价存量隐式库效果。
    """
    import uuid as _uuid

    from app.models.library_direction import DirectionLibrary
    from app.models.project import Project
    from app.services.libraries import get_library_for_project, set_source_libraries

    pid = project_id if isinstance(project_id, _uuid.UUID) else _uuid.UUID(str(project_id))
    library = await get_library_for_project(session, pid)
    if library is not None:
        return library
    project = await session.get(Project, pid)
    library = DirectionLibrary(
        name=project.name if project else "test-lib",
        project_id=pid,
        status="active",
        is_public=True,  # 课题起源库=共享库（P10：全实验室可读，等价存量 active 回填公共）
        created_by=None,
    )
    session.add(library)
    await session.flush()
    await set_source_libraries(session, topic_id=pid, library_ids=[library.id])
    return library


async def add_paper(session, *, project_id, **fields):
    """测试造数据统一入口：建内容池 Paper + 起源库成员行（+ 可选解读），返回 Paper。

    兼容旧单表口径：status/relevance_score 等判断字段自动落成员行，
    wiki_content/compiled_model 落论文级唯一解读行 paper_wikis。
    """
    from app.models.library_direction import LibraryPaper
    from app.models.paper import PaperWiki, new_paper

    member_kwargs = {k: fields.pop(k) for k in list(fields) if k in _MEMBERSHIP_FIELDS}
    member_kwargs.setdefault("status", "candidate")
    wiki_kwargs = {_WIKI_FIELDS[k]: fields.pop(k) for k in list(fields) if k in _WIKI_FIELDS}
    compiled_at = fields.pop("compiled_at", None)
    library = await ensure_project_library(session, project_id)
    paper = new_paper(**fields)
    session.add(paper)
    await session.flush()
    session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, **member_kwargs))
    if wiki_kwargs.get("content"):
        wiki = PaperWiki(paper_id=paper.id, **wiki_kwargs)
        if compiled_at is not None:
            wiki.created_at = wiki.updated_at = compiled_at
        session.add(wiki)
        paper.wiki = wiki
    await session.flush()
    return paper


async def wiki_of(session, *, paper_id):
    """取论文的唯一解读行 PaperWiki（断言解读内容/模型/编译者用；没有则 None）。"""
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.paper import PaperWiki

    pid = paper_id if isinstance(paper_id, _uuid.UUID) else _uuid.UUID(str(paper_id))
    return (
        await session.execute(select(PaperWiki).where(PaperWiki.paper_id == pid))
    ).scalar_one_or_none()


async def membership_of(session, *, project_id, paper_id):
    """取论文在某课题起源库的成员行（断言判断字段用）。"""
    import uuid as _uuid

    from app.services.libraries import get_membership

    library = await ensure_project_library(session, project_id)
    return await get_membership(
        session,
        library_id=library.id,
        paper_id=paper_id if isinstance(paper_id, _uuid.UUID) else _uuid.UUID(str(paper_id)),
    )


async def project_paper_rows(session, *, project_id):
    """某课题起源库的 (Paper, LibraryPaper) 全量列表（测试断言判断字段用）。"""
    from sqlalchemy import select

    from app.models.library_direction import LibraryPaper
    from app.models.paper import Paper

    library = await ensure_project_library(session, project_id)
    rows = (
        await session.execute(
            select(Paper, LibraryPaper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(LibraryPaper.library_id == library.id)
        )
    ).all()
    return [(paper, membership) for paper, membership in rows]


async def project_concepts(session, *, project_id):
    """某课题起源库的概念列表（概念不属于库：按库内论文的关联推导，同后端口径）。"""
    from sqlalchemy import select

    from app.models.paper import Concept
    from app.services.concepts import library_concept_ids

    library = await ensure_project_library(session, project_id)
    return list(
        (
            await session.execute(
                select(Concept).where(Concept.id.in_(library_concept_ids([library.id])))
            )
        ).scalars()
    )


async def add_concept(session, *, project_id, paper_id=None, **fields):
    """建概念（全平台一份，不挂库）；给了 paper_id 就挂到那篇论文上。

    「库有哪些概念」按库内论文的关联推导，所以要让概念出现在某个库/课题作用域里，
    必须关联一篇该库的论文。"""
    from app.models.paper import Concept, paper_concepts

    await ensure_project_library(session, project_id)
    concept = Concept(**fields)
    session.add(concept)
    await session.flush()
    if paper_id is not None:
        await session.execute(
            paper_concepts.insert().values(paper_id=paper_id, concept_id=concept.id)
        )
    return concept


async def make_project_with_library(
    client, headers, *, name="wiki-proj", statement=None, definition=None
):
    """建课题 + 一条 active 起源库（可带 definition）+ 关联，返回 (project_id, library_id)。

    P9c 起 create_project 不建库；依赖库语料/ingest 的 API 测试经此显式配库
    （project_id 回指 + 关联，等价存量隐式库）。definition 同时镜像标量列，供
    ingest 在 definition 为空时的回退读取。project_id 以 str 返回（与 API 一致）。
    """
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.services.libraries import set_source_libraries

    payload: dict = {"name": name}
    if statement is not None:
        payload["statement"] = statement
    resp = await client.post("/api/projects", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = _uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        library = DirectionLibrary(
            name=name,
            definition=definition,
            project_id=project_id,
            status="active",
            is_public=True,  # 课题起源库=共享库（P10：全实验室可读）
            created_by=None,
        )
        if definition:
            library.statement = definition.get("statement")
            library.rubric = definition.get("rubric")
            library.anchors = definition.get("anchor_papers")
            library.cadence = definition.get("cadence")
        session.add(library)
        await session.flush()
        await set_source_libraries(session, topic_id=project_id, library_ids=[library.id])
        await session.commit()
        library_id = library.id
    return str(project_id), library_id


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
