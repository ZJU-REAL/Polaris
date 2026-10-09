"""CORS 白名单：放行 Electron 桌面客户端与显式配置的来源，拒绝其余 origin。

背景：桌面端页面由自定义 app:// scheme 加载，请求后端属于跨域，且每个请求都带
Authorization 头 → 必然触发预检。以前 env=dev（桌面引擎的实际取值）是
allow_origins=["*"]，任何网页都能读本机引擎的响应（#850）；现在任何 env 都只认白名单。

Settings 走 lru_cache，所以这里显式构造一份 Settings 打进 app.main，绕开缓存。
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
from app.core.config import Settings
from app.main import DESKTOP_ORIGIN


@pytest.fixture
def make_client(monkeypatch):
    """按给定 env / cors_origins 构造应用客户端；cors_origins 模拟 POLARIS_CORS_ORIGINS。"""

    def make(cors_origins: str = "", env: str = "dev") -> AsyncClient:
        settings = Settings(env=env, cors_origins=cors_origins, allowed_hosts="test")
        monkeypatch.setattr(main_module, "get_settings", lambda: settings)
        return AsyncClient(
            transport=ASGITransport(app=main_module.create_app()), base_url="http://test"
        )

    return make


@pytest.mark.parametrize("env", ["dev", "prod"])
async def test_preflight_allows_desktop_origin(make_client, env):
    async with make_client(env=env) as c:
        res = await c.options(
            "/api/health",
            headers={
                "Origin": DESKTOP_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-polaris-session-secret",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == DESKTOP_ORIGIN


@pytest.mark.parametrize("env", ["dev", "prod"])
async def test_preflight_rejects_unknown_origin(make_client, env):
    async with make_client(env=env) as c:
        res = await c.options(
            "/api/health",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


async def test_preflight_allows_configured_origin(make_client):
    async with make_client("http://localhost:5173, https://alt.example.edu") as c:
        res = await c.options(
            "/api/health",
            headers={
                "Origin": "https://alt.example.edu",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "https://alt.example.edu"


@pytest.mark.parametrize("env", ["dev", "prod"])
async def test_simple_request_from_unknown_origin_gets_no_acao(make_client, env):
    """非预检请求本身仍然通过（CORS 是浏览器侧强制），但不回 ACAO 头。"""
    async with make_client(env=env) as c:
        res = await c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers


async def test_default_client_never_answers_wildcard(client):
    """conftest 的 client 就是 env=dev：以前这里回 "*"，现在恶意来源拿不到任何 ACAO。"""
    res = await client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.headers.get("access-control-allow-origin") is None
    res = await client.get("/api/health", headers={"Origin": DESKTOP_ORIGIN})
    assert res.headers.get("access-control-allow-origin") == DESKTOP_ORIGIN


def test_cors_origin_list_strips_and_drops_blanks():
    settings = Settings(cors_origins=" a , ,b ")
    assert settings.cors_origin_list == ["a", "b"]
    assert settings.allowed_origin_list == [DESKTOP_ORIGIN, "a", "b"]
    assert Settings(cors_origins="").cors_origin_list == []
