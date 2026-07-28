"""任务分层：建库 / 增量更新归实验室，其余归课题。

课题的任务列表不含库任务（去实验室工作台看）；库任务对够得着这个库的人可见，
不再要求「是起源课题的成员」——独立库根本没有起源课题。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.idea import Idea
from app.models.library_direction import (
    DirectionLibrary,
    DirectionLibraryCurator,
    TopicSourceLibrary,
)
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import voyages as voyages_service
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one().id


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


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
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin_hdr)
    assert resp.status_code == 200, resp.text
    return lib_id


async def test_library_voyages_stay_out_of_the_topic_list(client):
    """课题任务列表不含建库 / 增量更新——哪怕它们还带着 project_id（存量任务）。"""
    admin_email = "vscope-admin@example.com"
    await _hdr(client, admin_email)  # 第一个注册者占掉平台 admin 位
    await _promote_admin(admin_email)
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
        runs = await voyages_service.list_voyages(
            session, user_id=owner_id, project_id=project_id
        )
    ids = {r.id for r in runs}
    assert topic_run in ids
    assert legacy_ingest not in ids
    assert legacy_bootstrap not in ids


async def test_standalone_library_voyage_is_visible_to_its_curator(client):
    """独立库的任务 project_id 为空——按课题成员 join 会整个漏掉，策展人得看得见。"""
    admin_email = "vscope-curator-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-curator-owner@example.com")
    lib_id = await _library(client, admin, owner, name="独立库·任务可见")

    run_id = await _make_run(kind="wiki_ingest", library_id=uuid.UUID(lib_id))

    curator_email = "vscope-curator@example.com"
    await _hdr(client, curator_email)
    curator_id = await _user_id(curator_email)
    stranger_email = "vscope-stranger@example.com"
    await _hdr(client, stranger_email)
    stranger_id = await _user_id(stranger_email)

    async with get_sessionmaker()() as session:
        # 还不是策展人：看不到
        runs = await voyages_service.list_voyages(session, user_id=curator_id)
        assert run_id not in {r.id for r in runs}

    async with get_sessionmaker()() as session:
        session.add(
            DirectionLibraryCurator(library_id=uuid.UUID(lib_id), user_id=curator_id)
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        runs = await voyages_service.list_voyages(session, user_id=curator_id)
        assert run_id in {r.id for r in runs}
        # 无关的人始终看不到
        runs = await voyages_service.list_voyages(session, user_id=stranger_id)
        assert run_id not in {r.id for r in runs}


async def test_platform_voyage_visible_and_openable_by_admin_only(client):
    """每日新论文抓取：两个作用域 id 都为空 —— admin 列表看得到、详情页打得开。

    详情鉴权原本是白名单式的（要么有 project_id 且是成员，要么有 library_id 且能管库），
    平台级任务两条都不满足会直接 404，admin 也不例外 —— 那样日志/SSE/取消/重试全废。
    """
    admin_email = "vscope-daily-admin@example.com"
    admin = await _hdr(client, admin_email)  # 第一个注册者占掉平台 admin 位
    await _promote_admin(admin_email)
    admin_id = await _user_id(admin_email)
    member = await _hdr(client, "vscope-daily-member@example.com")
    member_id = await _user_id("vscope-daily-member@example.com")

    run_id = await _make_run(kind="daily_feed_sync")
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.project_id is None and run.library_id is None

    async with get_sessionmaker()() as session:
        admin_user = await session.get(User, admin_id)
        member_user = await session.get(User, member_id)
        # 列表：admin 看得到（is_admin 子句），普通成员看不到
        runs = await voyages_service.list_voyages(session, user_id=admin_id)
        assert run_id in {r.id for r in runs}
        runs = await voyages_service.list_voyages(session, user_id=member_id)
        assert run_id not in {r.id for r in runs}
        # 详情：admin 拿得到，普通成员拿不到
        assert (
            await voyages_service.get_voyage(
                session, voyage_id=run_id, user_id=admin_id, user=admin_user
            )
        ) is not None
        assert (
            await voyages_service.get_voyage(
                session, voyage_id=run_id, user_id=member_id, user=member_user
            )
        ) is None
        # 只传 user_id（不传 user）的调用点也要认得出 admin
        assert (
            await voyages_service.get_voyage(session, voyage_id=run_id, user_id=admin_id)
        ) is not None
        assert (
            await voyages_service.get_voyage(session, voyage_id=run_id, user_id=member_id)
        ) is None

    # 走 HTTP 详情页：admin 200，普通成员 404
    resp = await client.get(f"/api/voyages/{run_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "daily_feed_sync"
    resp = await client.get(f"/api/voyages/{run_id}", headers=member)
    assert resp.status_code == 404


async def test_platform_voyage_stays_out_of_the_topic_list(client):
    """课题任务列表不含每日新论文抓取——它是全实验室的事，不属于任何课题。

    正常形态 project_id 就是空、本来也进不了课题列表；这里连「万一挂上了课题」也一并
    拦住（判据是 kind，与建库/增量更新同款）。
    """
    admin_email = "vscope-daily-topic-admin@example.com"
    await _hdr(client, admin_email)
    await _promote_admin(admin_email)
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
        runs = await voyages_service.list_voyages(
            session, user_id=admin_id, project_id=project_id
        )
    ids = {r.id for r in runs}
    assert platform_run not in ids
    assert attached_run not in ids


async def test_linked_library_alone_grants_nothing(client):
    """课题关联了某个库、但我管不了那个库：列表看不到它的建库任务，详情也打不开。

    以前列表把「课题关联的库」也算可见、详情却要库级写权限——列表看得到、点进去 404。
    """
    admin_email = "vscope-linked-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-linked-owner@example.com")
    lib_id = await _library(client, admin, owner, name="别人的库·被我课题关联")

    member_email = "vscope-linked-member@example.com"
    member = await _hdr(client, member_email)
    member_id = await _user_id(member_email)
    resp = await client.post(
        "/api/projects", json={"name": "借语料的课题", "statement": "s"}, headers=member
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    await _link_library(topic_id=project_id, library_id=uuid.UUID(lib_id))

    run_id = await _make_run(kind="wiki_ingest", library_id=uuid.UUID(lib_id))
    assert await _list_and_open(client, run_id=run_id, user_id=member_id, hdr=member) == (
        False,
        404,
    )

    # 对照：库的创建者（能管这个库）列表看得到、详情打得开
    owner_id = await _user_id("vscope-linked-owner@example.com")
    assert await _list_and_open(client, run_id=run_id, user_id=owner_id, hdr=owner) == (
        True,
        200,
    )


async def test_library_voyage_visible_to_whoever_started_it(client):
    """我亲手发起的建库任务：哪怕后来不再是该库策展人，列表仍看得到、详情仍打得开。"""
    admin_email = "vscope-starter-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-starter-owner@example.com")
    lib_id = await _library(client, admin, owner, name="离任策展人的库")

    starter_email = "vscope-starter@example.com"
    starter = await _hdr(client, starter_email)
    starter_id = await _user_id(starter_email)

    # 他不是创建者也不是策展人（当初是，后来被撤了）——只剩「这是我发起的那次运行」
    mine = await _make_run(
        kind="wiki_bootstrap", library_id=uuid.UUID(lib_id), created_by=starter_id
    )
    others = await _make_run(kind="wiki_bootstrap", library_id=uuid.UUID(lib_id))

    assert await _list_and_open(client, run_id=mine, user_id=starter_id, hdr=starter) == (
        True,
        200,
    )
    assert await _list_and_open(client, run_id=others, user_id=starter_id, hdr=starter) == (
        False,
        404,
    )


async def test_platform_voyage_stays_admin_only_for_its_starter(client):
    """平台级任务（每日新论文）不吃「我发起的」这条：两个作用域 id 都空 → 只有 admin。"""
    admin_email = "vscope-platform-starter-admin@example.com"
    await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    starter_email = "vscope-platform-starter@example.com"
    starter = await _hdr(client, starter_email)
    starter_id = await _user_id(starter_email)

    run_id = await _make_run(kind="daily_feed_sync", created_by=starter_id)
    assert await _list_and_open(client, run_id=run_id, user_id=starter_id, hdr=starter) == (
        False,
        404,
    )


async def test_topic_member_still_sees_topic_voyages(client):
    """课题任务口径没被收紧：课题成员照常在列表看到、点得开自己课题的任务。"""
    admin_email = "vscope-topic-admin@example.com"
    await _hdr(client, admin_email)
    await _promote_admin(admin_email)
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

    stranger_email = "vscope-topic-stranger@example.com"
    stranger = await _hdr(client, stranger_email)
    stranger_id = await _user_id(stranger_email)
    assert await _list_and_open(client, run_id=run_id, user_id=stranger_id, hdr=stranger) == (
        False,
        404,
    )


async def test_ingest_state_says_whether_the_task_can_be_opened(client):
    """公共库对所有人可读：只读访客看得到「正在建库」，但拿到的 can_open 是 false。"""
    admin_email = "vscope-canopen-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-canopen-owner@example.com")
    lib_id = await _library(client, admin, owner, name="公共库·状态可见任务不可点")

    run_id = await _make_run(kind="wiki_ingest", library_id=uuid.UUID(lib_id))
    reader = await _hdr(client, "vscope-canopen-reader@example.com")

    resp = await client.get(f"/api/libraries/{lib_id}/ingest/state", headers=reader)
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["running_voyage_id"] == str(run_id)  # 状态照常显示
    assert state["can_open_running_voyage"] is False  # 但不给跳转
    assert (await client.get(f"/api/voyages/{run_id}", headers=reader)).status_code == 404

    resp = await client.get(f"/api/libraries/{lib_id}/ingest/state", headers=owner)
    assert resp.json()["can_open_running_voyage"] is True


async def test_admin_sees_every_topic_that_owns_a_task_it_can_see(client):
    """管理员看得到全平台任务，就必须看得到这些任务所属的课题——两边口径要一致。

    任务列表对 admin 是全平台可见的，但 ``GET /projects`` 原来只列本人参与的课题：
    实验室工作台拿任务的 project_id 回查那份列表查空，分组标题就成了「未知课题」。
    """
    admin_email = "vscope-name-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-name-owner@example.com")

    resp = await client.post(
        "/api/projects", json={"name": "别人的课题", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    run_id = await _make_run(kind="idea_forge", project_id=project_id)

    # 任务看得到
    resp = await client.get("/api/voyages", headers=admin)
    assert resp.status_code == 200, resp.text
    assert str(run_id) in {v["id"] for v in resp.json()}

    # 它所属的课题也看得到（名字查得到），且点得开
    resp = await client.get("/api/projects", headers=admin)
    assert resp.status_code == 200, resp.text
    listed = {p["id"]: p["name"] for p in resp.json()}
    assert listed.get(str(project_id)) == "别人的课题"
    resp = await client.get(f"/api/projects/{project_id}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "别人的课题"


async def test_admin_can_open_content_inside_a_topic_it_has_not_joined(client):
    """admin 是最高权限：别人课题里的想法，列表看得到、点得开；普通外人两样都不行。"""
    admin_email = "vscope-content-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-content-owner@example.com")
    stranger = await _hdr(client, "vscope-content-stranger@example.com")

    resp = await client.post(
        "/api/projects", json={"name": "带想法的课题", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        idea = Idea(project_id=project_id, title="别人课题里的想法")
        session.add(idea)
        await session.commit()
        idea_id = idea.id

    resp = await client.get(f"/api/projects/{project_id}/ideas", headers=admin)
    assert resp.status_code == 200, resp.text
    assert str(idea_id) in {i["id"] for i in resp.json()}
    assert (await client.get(f"/api/ideas/{idea_id}", headers=admin)).status_code == 200

    assert (
        await client.get(f"/api/projects/{project_id}/ideas", headers=stranger)
    ).status_code == 404
    assert (await client.get(f"/api/ideas/{idea_id}", headers=stranger)).status_code == 404


async def test_non_member_still_sees_no_topic_they_have_no_right_to(client):
    """没权限的不展示：普通用户的课题列表里没有别人的课题，详情也打不开。"""
    admin_email = "vscope-noleak-admin@example.com"
    await _hdr(client, admin_email)
    await _promote_admin(admin_email)
    owner = await _hdr(client, "vscope-noleak-owner@example.com")
    stranger = await _hdr(client, "vscope-noleak-stranger@example.com")

    resp = await client.post(
        "/api/projects", json={"name": "不该外泄的课题", "statement": "s"}, headers=owner
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    resp = await client.get("/api/projects", headers=stranger)
    assert resp.status_code == 200, resp.text
    assert project_id not in {p["id"] for p in resp.json()}
    assert (await client.get(f"/api/projects/{project_id}", headers=stranger)).status_code == 404


async def test_new_ingest_voyage_carries_no_project(client):
    """新建的库任务不写 project_id：库任务归库，课题只是关联库来用语料。"""
    from app.schemas.ingest import IngestKnobs
    from app.services.ingest import create_ingest_voyage

    admin_email = "vscope-ingest-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)
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
        run = VoyageRun(kind="wiki_ingest", status="executing", goal="跑着的",
                        created_by=uuid.UUID(me["id"]))
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
    from app.core.db import get_sessionmaker
    from app.models.llm_config import LLMUsage
    from app.models.voyage import VoyageRun
    from sqlalchemy import select as _select

    token = await register_and_login(client, email="del2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()

    async with get_sessionmaker()() as session:
        run = VoyageRun(kind="wiki_ingest", status="done", goal="跑完的",
                        created_by=uuid.UUID(me["id"]))
        session.add(run)
        await session.flush()
        session.add(
            LLMUsage(
                stage="relevance", model="fake-default",
                prompt_tokens=100, completion_tokens=20, voyage_id=run.id,
            )
        )
        await session.commit()
        run_id = run.id

    assert (await client.delete(f"/api/voyages/{run_id}", headers=headers)).status_code == 204
    async with get_sessionmaker()() as session:
        usage = (await session.execute(_select(LLMUsage).where(LLMUsage.model == "fake-default"))).scalars().all()
    assert len(usage) == 1 and usage[0].voyage_id is None, "用量行必须留下，只解引用"
