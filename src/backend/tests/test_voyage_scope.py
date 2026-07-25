"""任务分层：建库 / 增量更新归实验室，其余归课题。

课题的任务列表不含库任务（去实验室工作台看）；库任务对够得着这个库的人可见，
不再要求「是起源课题的成员」——独立库根本没有起源课题。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator
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


async def _make_run(*, kind: str, library_id=None, project_id=None) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind=kind,
            goal=f"{kind} 测试任务",
            status="planning",
            cursor=0,
            library_id=library_id,
            project_id=project_id,
        )
        session.add(run)
        await session.commit()
        return run.id


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
