"""#850：老库里别的用户行的数据并到本地用户；本地用户在启动时就落定。

迁移 7d2e4f9a1b63 的数据层断言在 test_migrations.py；这里把同一个 ``consolidate``
跑在 metadata 建出的测试库上，从 API 看：合并之后 local-session 拿到的人看得见旧账号
建的课题、对话、SSH 凭据、笔记。
"""

import datetime as dt
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from fastapi_users.password import PasswordHelper
from sqlalchemy import select, update

from app.core.db import get_engine, get_sessionmaker
from app.models.user import User
from app.services.local_user import (
    LOCAL_USER_EMAIL,
    _free_username,
    ensure_local_user,
    local_user_id,
)
from tests.conftest import add_paper, register_and_login

FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nk\n-----END OPENSSH PRIVATE KEY-----\n"
MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "7d2e4f9a1b63_consolidate_user_rows.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("consolidate_user_rows", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent_on(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


def _user(email: str, *, username: str | None, created_at: dt.datetime, active: bool = True):
    return User(
        email=email,
        hashed_password=PasswordHelper().hash("x"),
        is_active=active,
        is_superuser=False,
        is_verified=True,
        display_name=email,
        username=username,
        created_at=created_at,
    )


async def _headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {await register_and_login(client)}"}


async def test_registered_account_data_is_visible_after_consolidation(agent_on, client):
    # 1. 以「旧账号」身份经 API 建数据：先拿本地会话建，再把这一行改成一个更早的注册账号
    headers = await _headers(client)
    resp = await client.post("/api/projects", json={"name": "old-proj"}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    project_id = resp.json()["id"]
    resp = await client.post(
        "/api/ssh-credentials",
        json={
            "name": "gpu",
            "host": "gpu.lab",
            "port": 22,
            "username": "me",
            "private_key": FAKE_PEM,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), title="Old Paper")
        await session.commit()
        paper_id = str(paper.id)
    resp = await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "old note"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/chat/conversations", json={"scope_kind": "global"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    conversation_id = resp.json()["id"]

    async with get_sessionmaker()() as session:
        old = await session.scalar(select(User).where(User.email == LOCAL_USER_EMAIL))
        old.email = "me@x.com"
        old.username = "me"
        old.created_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        old.settings = {"daily.sync_time": "07:15"}
        # 2. 后来某次启动建出了一个空的 local@
        session.add(
            _user(
                LOCAL_USER_EMAIL,
                username="local",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            )
        )
        await session.commit()
        old_id = old.id

    headers = await _headers(client)
    # 合并前：新 local@ 看不到旧账号的课题（这正是 #850 的缺陷）
    before = (await client.get("/api/projects", headers=headers)).json()
    assert project_id not in {p["id"] for p in before}

    # 3. 合并
    module = _load_migration()
    async with get_engine().begin() as conn:
        await conn.run_sync(module.consolidate)

    headers = await _headers(client)
    me = (await client.get("/api/users/me", headers=headers)).json()
    assert me["email"] == LOCAL_USER_EMAIL and me["id"] != str(old_id)
    projects = (await client.get("/api/projects", headers=headers)).json()
    assert project_id in {p["id"] for p in projects}
    creds = (await client.get("/api/ssh-credentials", headers=headers)).json()
    assert [c["name"] for c in creds] == ["gpu"]
    notes = (await client.get(f"/api/papers/{paper_id}/notes", headers=headers)).json()
    assert [n["content"] for n in notes] == ["old note"]
    convs = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert conversation_id in {c["id"] for c in convs}

    async with get_sessionmaker()() as session:
        old = await session.get(User, old_id)
        await session.refresh(old)
        assert old.is_active is False
        local = await session.scalar(select(User).where(User.email == LOCAL_USER_EMAIL))
        assert local.settings == {"daily.sync_time": "07:15"}

    # 幂等：再跑一次什么都不变
    async with get_engine().begin() as conn:
        await conn.run_sync(module.consolidate)
    projects_again = (await client.get("/api/projects", headers=headers)).json()
    assert {p["id"] for p in projects_again} == {p["id"] for p in projects}


async def test_ensure_local_user_creates_when_empty(app):
    async with get_sessionmaker()() as session:
        user = await ensure_local_user(session)
        assert user.email == LOCAL_USER_EMAIL and user.username == "local" and user.is_active
        again = await ensure_local_user(session)
        assert again.id == user.id
        assert await local_user_id(session) == user.id


async def test_ensure_local_user_adopts_earliest_user_and_keeps_taken_username(client):
    async with get_sessionmaker()() as session:
        session.add_all(
            [
                _user("me@x.com", username="me", created_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC)),
                _user(
                    "other@x.com",
                    username="local",
                    created_at=dt.datetime(2025, 6, 1, tzinfo=dt.UTC),
                ),
            ]
        )
        await session.commit()
        me_id = await session.scalar(select(User.id).where(User.email == "me@x.com"))

    # local-session 认领最早的用户，而不是另建一个空用户
    headers = await _headers(client)
    me = (await client.get("/api/users/me", headers=headers)).json()
    assert me["id"] == str(me_id)
    assert me["email"] == LOCAL_USER_EMAIL
    assert me["username"] == "me"  # local 被别人占着：不抢
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(User).where(User.email == "me@x.com")) is None
        assert await local_user_id(session) == me_id


async def test_ensure_local_user_reactivates_an_inactive_local_user(client):
    async with get_sessionmaker()() as session:
        session.add(
            _user(
                LOCAL_USER_EMAIL,
                username="local",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                active=False,
            )
        )
        await session.commit()
    headers = await _headers(client)
    me = await client.get("/api/users/me", headers=headers)
    assert me.status_code == 200 and me.json()["is_active"] is True


async def test_free_username_skips_taken_names(app):
    async with get_sessionmaker()() as session:
        assert await _free_username(session) == "local"
        session.add(_user("a@x.com", username="local", created_at=dt.datetime(2025, 1, 1)))
        await session.commit()
        assert await _free_username(session) == "local_2"
        session.add(_user("b@x.com", username="local_2", created_at=dt.datetime(2025, 1, 2)))
        await session.commit()
        assert await _free_username(session) == "local_3"
        a_id = await session.scalar(select(User.id).where(User.email == "a@x.com"))
        # 自己占着的名字不算被占
        assert await _free_username(session, exclude=a_id) == "local"
        await session.execute(update(User).where(User.id == a_id).values(username=None))
        await session.commit()
        assert await _free_username(session) == "local"
