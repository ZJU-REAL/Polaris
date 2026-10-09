"""任务分层：建库 / 增量更新归实验室，其余归课题。

课题的任务列表不含库任务（去实验室工作台看）；库任务对够得着这个库的人可见，
不再要求「是起源课题的成员」——独立库根本没有起源课题。
"""

import uuid

from sqlalchemy import select

from app.api.auth import LOCAL_USER_EMAIL
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, TopicSourceLibrary
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import voyages as voyages_service
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            (await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL)))
            .scalar_one()
            .id
        )


async def _make_run(*, kind: str, library_id=None, project_id=None, created_by=None) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind=kind,
            goal=f"{kind} 测试任务",
            status="planning",
            cursor=0,
            library_id=library_id,
            project_id=project_id,
            created_by=created_by,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _link_library(*, topic_id: uuid.UUID, library_id: uuid.UUID) -> None:
    """把库关联给课题（课题拿它的语料用，但管不了它）。"""
    async with get_sessionmaker()() as session:
        session.add(TopicSourceLibrary(topic_id=topic_id, library_id=library_id))
        await session.commit()


async def _list_and_open(client, *, run_id: uuid.UUID, user_id: uuid.UUID, hdr) -> tuple[bool, int]:
    """(该任务在我的任务列表里吗, 详情页 HTTP 状态码) —— 两者必须一致。"""
    async with get_sessionmaker()() as session:
        runs = await voyages_service.list_voyages(session, user_id=user_id)
        listed = run_id in {r.id for r in runs}
    resp = await client.get(f"/api/voyages/{run_id}", headers=hdr)
    return listed, resp.status_code


async def _library(client, admin_hdr, owner_hdr, *, name) -> str:
    resp = await client.post(
        "/api/libraries", json={"name": name, "statement": "测试库"}, headers=owner_hdr
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]
    # 转公共审批流已移除（#593）：直接置 is_public 复原「已批准的公共独立库」语义
    # （本文件多个用例的前提是公共库对全员可读，如 can_open 一例）
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        lib.is_public = True
        await session.commit()
    return lib_id


async def test_library_voyages_stay_out_of_the_topic_list(client):
    """课题任务列表不含建库 / 增量更新——哪怕它们还带着 project_id（存量任务）。"""
    admin_email = "vscope-admin@example.com"
    await _hdr(client, admin_email)  # 第一个注册者占掉平台 admin 位
    owner = await _hdr(client, "vscope-owner@example.com")
    owner_id = await _user_id("vscope-owner@example.com")

    resp = await client.post(
        "/api/projects", json={"name": "任务分层课题", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    topic_run = await _make_run(kind="idea_forge", project_id=project_id)
    # 存量形态：库任务只挂课题，没有 library_id —— 按 library_id 判会漏，按 kind 才拦得住
    legacy_ingest = await _make_run(kind="wiki_ingest", project_id=project_id)
    legacy_bootstrap = await _make_run(kind="wiki_bootstrap", project_id=project_id)

    async with get_sessionmaker()() as session:
        runs = await voyages_service.list_voyages(session, user_id=owner_id, project_id=project_id)
    ids = {r.id for r in runs}
    assert topic_run in ids
    assert legacy_ingest not in ids
    assert legacy_bootstrap not in ids


async def test_standalone_library_voyage_is_visible_to_its_creator(client):
    """独立库的任务 project_id 为空——按课题成员 join 会整个漏掉，创建者得看得见。"""
    admin_email = "vscope-curator-admin@example.com"
    admin = await _hdr(client, admin_email)
    owner_email = "vscope-curator-owner@example.com"
    owner = await _hdr(client, owner_email)
    owner_id = await _user_id(owner_email)
    lib_id = await _library(client, admin, owner, name="独立库·任务可见")

    run_id = await _make_run(kind="wiki_ingest", library_id=uuid.UUID(lib_id))

    async with get_sessionmaker()() as session:
        # 库创建者看得到自己库的任务
        runs = await voyages_service.list_voyages(session, user_id=owner_id)
        assert run_id in {r.id for r in runs}


async def test_platform_voyage_visible_and_openable_by_any_user(client):
    """每日新论文抓取：两个作用域 id 都为空 —— 任何登录用户列表看得到、详情页打得开。

    admin-only 口径随 role 治理移除（#614）：单机档位登录即主人，平台级任务本来就是
    替他跑的；日志/SSE/取消/重试都不该被白名单式鉴权挡成 404。
    """
    first = await _hdr(client, "vscope-daily-first@example.com")
    first_id = await _user_id("vscope-daily-first@example.com")
    member = await _hdr(client, "vscope-daily-member@example.com")
    member_id = await _user_id("vscope-daily-member@example.com")

    run_id = await _make_run(kind="daily_feed_sync")
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.project_id is None and run.library_id is None

    async with get_sessionmaker()() as session:
        first_user = await session.get(User, first_id)
        member_user = await session.get(User, member_id)
        # 列表：所有登录用户都看得到（platform_scoped 子句）
        runs = await voyages_service.list_voyages(session, user_id=first_id)
        assert run_id in {r.id for r in runs}
        runs = await voyages_service.list_voyages(session, user_id=member_id)
        assert run_id in {r.id for r in runs}
        # 详情：两条调用路径（带 user / 只带 user_id）都拿得到
        assert (
            await voyages_service.get_voyage(
                session, voyage_id=run_id, user_id=first_id, user=first_user
            )
        ) is not None
        assert (
            await voyages_service.get_voyage(
                session, voyage_id=run_id, user_id=member_id, user=member_user
            )
        ) is not None
        assert (
            await voyages_service.get_voyage(session, voyage_id=run_id, user_id=member_id)
        ) is not None

    # 走 HTTP 详情页：谁都 200
    resp = await client.get(f"/api/voyages/{run_id}", headers=first)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "daily_feed_sync"
    resp = await client.get(f"/api/voyages/{run_id}", headers=member)
    assert resp.status_code == 200


async def test_platform_voyage_stays_out_of_the_topic_list(client):
    """课题任务列表不含每日新论文抓取——它是全实验室的事，不属于任何课题。

    正常形态 project_id 就是空、本来也进不了课题列表；这里连「万一挂上了课题」也一并
    拦住（判据是 kind，与建库/增量更新同款）。
    """
    admin_email = "vscope-daily-topic-admin@example.com"
    await _hdr(client, admin_email)
    admin_id = await _user_id(admin_email)
    owner = await _hdr(client, "vscope-daily-owner@example.com")

    resp = await client.post(
        "/api/projects", json={"name": "每日任务不入课题", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    platform_run = await _make_run(kind="daily_feed_sync")
    attached_run = await _make_run(kind="daily_feed_sync", project_id=project_id)

    async with get_sessionmaker()() as session:
        runs = await voyages_service.list_voyages(session, user_id=admin_id, project_id=project_id)
    ids = {r.id for r in runs}
    assert platform_run not in ids
    assert attached_run not in ids


async def test_platform_voyage_visible_to_its_starter(client):
    """平台级任务对所有登录用户可见（#614）：发起者自然列表看得到、详情打得开。"""
    await _hdr(client, "vscope-platform-starter-first@example.com")
    starter_email = "vscope-platform-starter@example.com"
    starter = await _hdr(client, starter_email)
    starter_id = await _user_id(starter_email)

    run_id = await _make_run(kind="daily_feed_sync", created_by=starter_id)
    assert await _list_and_open(client, run_id=run_id, user_id=starter_id, hdr=starter) == (
        True,
        200,
    )


async def test_topic_member_still_sees_topic_voyages(client):
    """课题任务口径没被收紧：课题成员照常在列表看到、点得开自己课题的任务。"""
    admin_email = "vscope-topic-admin@example.com"
    await _hdr(client, admin_email)
    owner_email = "vscope-topic-owner@example.com"
    owner = await _hdr(client, owner_email)
    owner_id = await _user_id(owner_email)

    resp = await client.post(
        "/api/projects", json={"name": "课题任务照常可见", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    # 别人发起的课题任务也看得到——凭的是课题成员身份
    run_id = await _make_run(kind="idea_forge", project_id=project_id)
    assert await _list_and_open(client, run_id=run_id, user_id=owner_id, hdr=owner) == (
        True,
        200,
    )


async def test_ingest_state_says_whether_the_task_can_be_opened(client):
    """建库状态带上正在跑的任务，并告诉前端能不能点进去。"""
    admin_email = "vscope-canopen-admin@example.com"
    admin = await _hdr(client, admin_email)
    owner = await _hdr(client, "vscope-canopen-owner@example.com")
    lib_id = await _library(client, admin, owner, name="公共库·状态可见任务不可点")

    run_id = await _make_run(kind="wiki_ingest", library_id=uuid.UUID(lib_id))

    resp = await client.get(f"/api/libraries/{lib_id}/ingest/state", headers=owner)
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["running_voyage_id"] == str(run_id)
    assert state["can_open_running_voyage"] is True
    assert (await client.get(f"/api/voyages/{run_id}", headers=owner)).status_code == 200


async def test_new_ingest_voyage_carries_no_project(client):
    """新建的库任务不写 project_id：库任务归库，课题只是关联库来用语料。"""
    from app.schemas.ingest import IngestKnobs
    from app.services.ingest import create_ingest_voyage

    admin_email = "vscope-ingest-admin@example.com"
    admin = await _hdr(client, admin_email)
    owner = await _hdr(client, "vscope-ingest-owner@example.com")
    lib_id = await _library(client, admin, owner, name="建库任务归属")

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        run = await create_ingest_voyage(
            session,
            library=library,
            project=None,
            mode="bootstrap",
            knobs=IngestKnobs(),
            created_by=None,
        )
        assert run.project_id is None
        assert run.library_id == uuid.UUID(lib_id)


async def test_delete_voyage_only_when_finished(client):
    """删除只允许删已结束的任务；还在跑的先取消。

    还在跑就删的话，worker 那边仍在按这个 id 执行，行没了会一路报错到不知所云的地方。
    """
    from app.core.db import get_sessionmaker
    from app.models.voyage import VoyageRun

    token = await register_and_login(client, email="del@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()

    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_ingest", status="executing", goal="跑着的", created_by=uuid.UUID(me["id"])
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    resp = await client.delete(f"/api/voyages/{run_id}", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "VOYAGE_STILL_RUNNING"

    # 取消后可删
    assert (await client.post(f"/api/voyages/{run_id}/cancel", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/voyages/{run_id}", headers=headers)).status_code == 204
    async with get_sessionmaker()() as session:
        assert await session.get(VoyageRun, run_id) is None


async def test_delete_voyage_keeps_token_accounting(client):
    """删任务不该把它花过的 token 从账上抹掉——用量行只解引用，不删。"""
    from sqlalchemy import select as _select

    from app.core.db import get_sessionmaker
    from app.models.llm_config import LLMUsage
    from app.models.voyage import VoyageRun

    token = await register_and_login(client, email="del2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()

    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_ingest", status="done", goal="跑完的", created_by=uuid.UUID(me["id"])
        )
        session.add(run)
        await session.flush()
        session.add(
            LLMUsage(
                stage="relevance",
                model="fake-default",
                prompt_tokens=100,
                completion_tokens=20,
                voyage_id=run.id,
            )
        )
        await session.commit()
        run_id = run.id

    assert (await client.delete(f"/api/voyages/{run_id}", headers=headers)).status_code == 204
    async with get_sessionmaker()() as session:
        stmt = _select(LLMUsage).where(LLMUsage.model == "fake-default")
        usage = (await session.execute(stmt)).scalars().all()
    assert len(usage) == 1 and usage[0].voyage_id is None, "用量行必须留下，只解引用"
