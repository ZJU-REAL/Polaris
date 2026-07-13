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

from app.core.db import Base, dispose_engine, get_engine  # noqa: E402
from app.main import create_app  # noqa: E402

import app.models  # noqa: E402,F401  isort: skip  注册全部表


@pytest_asyncio.fixture
async def app():
    """每个测试一套干净的表（ASGITransport 不跑 lifespan，手动建表）。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield create_app()
    await dispose_engine()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


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
