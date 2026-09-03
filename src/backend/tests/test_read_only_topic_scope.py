"""只读账号（游客）只看得见别人把它加进去的课题，不是全实验室的课题清单。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.project import ProjectMember
from app.models.user import User
from tests.conftest import register_and_login, set_user_fields


async def _make_guest(client) -> str:
    # 管理端建号 API 已随去实验室化移除（#603）：正常注册后直接写库摆成游客。
    token = await register_and_login(client, email="guest@example.com", username="guest")
    await set_user_fields("guest@example.com", role="admin", read_only=True)
    return token


async def _add_guest_to(project_id: str) -> None:
    async with get_sessionmaker()() as session:
        guest_id = (
            await session.execute(select(User.id).where(User.username == "guest"))
        ).scalar_one()
        session.add(
            ProjectMember(
                project_id=uuid.UUID(project_id), user_id=guest_id, role="member"
            )
        )
        await session.commit()


async def test_guest_sees_only_the_topics_it_was_added_to(client):
    admin = await register_and_login(client)
    ah = {"Authorization": f"Bearer {admin}"}

    shown = (
        await client.post(
            "/api/projects", json={"name": "Shown", "statement": "the demo topic"}, headers=ah
        )
    ).json()
    hidden = await client.post(
        "/api/projects", json={"name": "Hidden", "statement": "someone else's work"}, headers=ah
    )
    assert hidden.status_code in (200, 201)

    guest = await _make_guest(client)
    gh = {"Authorization": f"Bearer {guest}"}

    # 还没被加进任何课题：一个也看不到（此前顶着 role=admin 能看到全部）
    listed = await client.get("/api/projects", headers=gh)
    assert listed.status_code == 200
    assert listed.json() == []

    await _add_guest_to(shown["id"])

    listed = await client.get("/api/projects", headers=gh)
    assert listed.status_code == 200
    names = [p["name"] for p in listed.json()]
    assert names == ["Shown"], names

    # 够不着的那个点进去也是 404，不是「列表里没有、直连能开」
    assert (await client.get(f"/api/projects/{hidden.json()['id']}", headers=gh)).status_code == 404


async def test_real_admins_still_see_every_topic(client):
    """收紧的只是只读账号：真管理员照旧全实验室可见，否则任务列表会认不出课题。"""
    admin = await register_and_login(client)
    ah = {"Authorization": f"Bearer {admin}"}
    for name in ("A", "B"):
        assert (
            await client.post("/api/projects", json={"name": name, "statement": name}, headers=ah)
        ).status_code in (200, 201)

    # 管理端建号 API 已移除：注册第二个账号后直接写库升为 admin
    token = await register_and_login(client, email="other@example.com", username="other_admin")
    await set_user_fields("other@example.com", role="admin")

    listed = await client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200
    assert {p["name"] for p in listed.json()} >= {"A", "B"}
