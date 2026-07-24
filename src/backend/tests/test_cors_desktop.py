"""prod 下的 CORS 白名单：放行 Electron 桌面客户端，拒绝其余 origin。

背景：桌面端页面由自定义 app:// scheme 加载，请求后端属于跨域，且每个请求都带
Authorization 头 → 必然触发预检。改造前 prod 是 allow_origins=[]，预检直接 400，
桌面端全部 API 不可用。

conftest 把 POLARIS_ENV 钉成 dev 且 Settings 走 lru_cache，所以这里显式构造一份
prod Settings 打进 app.main，绕开缓存。
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
from app.core.config import Settings
from app.main import DESKTOP_ORIGIN


@pytest.fixture
def prod_client(monkeypatch):
    """构造 env=prod 的应用客户端；cors_origins 模拟 POLARIS_CORS_ORIGINS。"""

    def make(cors_origins: str = "") -> AsyncClient:
        settings = Settings(env="prod", cors_origins=cors_origins)
        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        return AsyncClient(
            transport=ASGITransport(app=main_module.create_app()), base_url="http://test"
        )

    return make


async def test_preflight_allows_desktop_origin(prod_client):
    async with prod_client() as c:
        res = await c.options(
            "/api/health",
            headers={
                "Origin": DESKTOP_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == DESKTOP_ORIGIN


async def test_preflight_rejects_unknown_origin(prod_client):
    async with prod_client() as c:
        res = await c.options(
            "/api/health",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


async def test_preflight_allows_configured_origin(prod_client):
    async with prod_client("https://polaris.example.edu, https://alt.example.edu") as c:
        res = await c.options(
            "/api/health",
            headers={
                "Origin": "https://alt.example.edu",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "https://alt.example.edu"


async def test_simple_request_from_unknown_origin_gets_no_acao(prod_client):
    """非预检请求本身仍然通过（CORS 是浏览器侧强制），但不回 ACAO 头。"""
    async with prod_client() as c:
        res = await c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers


async def test_dev_still_allows_any_origin(client):
    """dev 行为不变：conftest 的 client 就是 env=dev。"""
    res = await client.options(
        "/api/health",
        headers={"Origin": "https://anything.example", "Access-Control-Request-Method": "GET"},
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "*"


def test_cors_origin_list_strips_and_drops_blanks():
    assert Settings(env="prod", cors_origins=" a , ,b ").cors_origin_list == ["a", "b"]
    assert Settings(env="prod", cors_origins="").cors_origin_list == []
