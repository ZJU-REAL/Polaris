"""本机引擎的防护（#850）：Host 白名单、本地会话口令/Origin 规则、每份安装独立密钥。

背景：引擎监听 127.0.0.1，以前 CORS 放 "*"、/auth/local-session 谁调都发 30 天会话、
密钥是公开默认值——任何网页（含 DNS rebinding）都能拿到会话，进而注册 MCP/ACP
命令在用户机器上执行代码。
"""

import base64
import hashlib
import hmac
import uuid

import pytest
from cryptography.fernet import Fernet, InvalidToken
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import security
from app.core.config import DESKTOP_ORIGIN, LEGACY_DEFAULT_SECRET_KEY, get_settings
from app.core.db import get_sessionmaker
from app.core.hosts import host_without_port
from app.models.download_client import DownloadApiKey
from app.models.llm_config import LLMProviderConfig
from app.models.mcp_server import McpServer
from app.models.system_setting import SystemSetting

# ---- Host 白名单 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:18080", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:18080", "::1"),
        ("[::1]", "::1"),
        ("evil.example:80", "evil.example"),
        ("", ""),
    ],
)
def test_host_without_port(raw, expected):
    assert host_without_port(raw) == expected


@pytest.mark.parametrize("host", ["127.0.0.1:18080", "localhost:5173", "[::1]:18080", "localhost"])
async def test_loopback_hosts_are_accepted(client, host):
    res = await client.get("/api/health", headers={"Host": host})
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("host", ["evil.example", "evil.example:18080", "192.168.1.5:18080"])
async def test_foreign_host_header_is_rejected(client, host):
    """DNS rebinding：恶意域名解析到 127.0.0.1 后 Host 头仍是那个域名。"""
    res = await client.get("/api/health", headers={"Host": host})
    assert res.status_code == 400
    res = await client.post("/api/auth/local-session", headers={"Host": host})
    assert res.status_code == 400


async def test_allowed_hosts_star_disables_the_check(monkeypatch):
    import app.main as main_module
    from app.core.config import Settings

    settings = Settings(allowed_hosts="*")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    transport = ASGITransport(app=main_module.create_app())
    async with AsyncClient(transport=transport, base_url="http://anything.example") as c:
        assert (await c.get("/api/health")).status_code == 200


# ---- /api/health 的实例标识 ----


async def test_health_echoes_instance_id(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "instance_id", "abc123", raising=False)
    body = (await client.get("/api/health")).json()
    assert body["instance_id"] == "abc123"


# ---- 本地会话 ----


async def test_local_session_requires_the_launch_secret_when_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_session_secret", "s3cret-per-launch", raising=False)

    missing = await client.post("/api/auth/local-session")
    assert missing.status_code == 403
    assert missing.json()["detail"] == "LOCAL_SESSION_SECRET_INVALID"

    wrong = await client.post(
        "/api/auth/local-session", headers={"X-Polaris-Session-Secret": "guess"}
    )
    assert wrong.status_code == 403

    # 口令配置了就只认口令：来自桌面 origin 也不能免
    desktop_only = await client.post("/api/auth/local-session", headers={"Origin": DESKTOP_ORIGIN})
    assert desktop_only.status_code == 403

    ok = await client.post(
        "/api/auth/local-session",
        headers={"X-Polaris-Session-Secret": "s3cret-per-launch", "Origin": DESKTOP_ORIGIN},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["access_token"]


async def test_local_session_without_secret_rejects_foreign_origins(client):
    """从源码跑（没配口令）：别的网站的页面拿不到会话。"""
    res = await client.post("/api/auth/local-session", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert res.json()["detail"] == "LOCAL_SESSION_ORIGIN_FORBIDDEN"
    res = await client.post("/api/auth/local-session", headers={"Origin": "null"})
    assert res.status_code == 403


async def test_local_session_without_secret_allows_no_origin_and_allowed_origins(
    client, monkeypatch
):
    # 命令行/脚本/同源代理：不带 Origin
    assert (await client.post("/api/auth/local-session")).status_code == 200
    # 桌面客户端恒在白名单
    res = await client.post("/api/auth/local-session", headers={"Origin": DESKTOP_ORIGIN})
    assert res.status_code == 200
    # 开发服务器直连：显式配置 POLARIS_CORS_ORIGINS 后放行
    dev = "http://localhost:5173"
    assert (
        await client.post("/api/auth/local-session", headers={"Origin": dev})
    ).status_code == 403
    monkeypatch.setattr(get_settings(), "cors_origins", dev, raising=False)
    res = await client.post("/api/auth/local-session", headers={"Origin": dev})
    assert res.status_code == 200


# ---- 加密密钥：旧密文照常解得开，启动轮换换成新密钥 ----


def _legacy_fernet() -> Fernet:
    digest = hashlib.sha256(LEGACY_DEFAULT_SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@pytest.fixture
def per_install_key(monkeypatch):
    """模拟桌面外壳传入的每份安装独立密钥。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "per-install-secret-0123456789", raising=False)
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode(), raising=False)
    security.get_fernet.cache_clear()
    yield settings.encryption_key
    security.get_fernet.cache_clear()


def test_legacy_ciphertext_still_decrypts_with_per_install_key(per_install_key):
    legacy = _legacy_fernet().encrypt(b"sk-legacy").decode()
    assert security.decrypt_secret(legacy) == "sk-legacy"
    assert security.needs_rotation(legacy)

    fresh = security.encrypt_secret("sk-new")
    # 新写入的只用主密钥：旧的公开密钥解不开
    with pytest.raises(InvalidToken):
        _legacy_fernet().decrypt(fresh.encode())
    assert Fernet(per_install_key.encode()).decrypt(fresh.encode()) == b"sk-new"
    assert not security.needs_rotation(fresh)


def test_unknown_ciphertext_still_fails_closed(per_install_key):
    other = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    with pytest.raises(InvalidToken):
        security.decrypt_secret(other)
    assert security.rotate_secret(other) is None


async def test_rotation_reencrypts_all_legacy_values_and_is_idempotent(app, per_install_key):
    legacy = _legacy_fernet()
    old_key = legacy.encrypt(b"sk-provider").decode()
    old_env = legacy.encrypt(b'{"TOKEN": "t"}').decode()
    bare = legacy.encrypt(b"s2-key").decode()
    in_entry = legacy.encrypt(b"s2-key-2").decode()
    mineru = legacy.encrypt(b"mineru-key").decode()
    stranger = Fernet(Fernet.generate_key()).encrypt(b"lost").decode()

    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="p", kind="openai_compat", api_key_encrypted=old_key)
        broken = LLMProviderConfig(name="b", kind="openai_compat", api_key_encrypted=stranger)
        server = McpServer(slug="srv", title="srv", transport="stdio", env_encrypted=old_env)
        session.add_all([provider, broken, server])
        session.add(
            SystemSetting(
                key="literature_search",
                value={
                    "provider_keys": {
                        "semantic_scholar": [bare, {"id": "x", "secret": in_entry, "enabled": True}]
                    }
                },
            )
        )
        session.add(
            SystemSetting(key="document_processing", value={"mineru_credentials": [mineru]})
        )
        await session.commit()
        provider_id, broken_id, server_id = provider.id, broken.id, server.id

    async with get_sessionmaker()() as session:
        changed = await security.rotate_encrypted_secrets(session)
    assert changed == 4  # provider + mcp env + 两份设置文档（各算一次）

    primary = Fernet(per_install_key.encode())
    async with get_sessionmaker()() as session:
        p = await session.get(LLMProviderConfig, provider_id)
        assert primary.decrypt(p.api_key_encrypted.encode()) == b"sk-provider"
        # 谁都解不开的不动（也不删）
        assert (await session.get(LLMProviderConfig, broken_id)).api_key_encrypted == stranger
        s = await session.get(McpServer, server_id)
        assert primary.decrypt(s.env_encrypted.encode()) == b'{"TOKEN": "t"}'

        lit = (await session.get(SystemSetting, "literature_search")).value
        items = lit["provider_keys"]["semantic_scholar"]
        # 旧式裸字符串被展开成对象，并钉住它原来（按密文现算）的 id
        expected_id = uuid.uuid5(uuid.NAMESPACE_URL, f"polaris:semantic_scholar:0:{bare}")
        assert items[0]["id"] == str(expected_id)
        assert primary.decrypt(items[0]["secret"].encode()) == b"s2-key"
        assert items[1]["id"] == "x"
        assert primary.decrypt(items[1]["secret"].encode()) == b"s2-key-2"
        doc = (await session.get(SystemSetting, "document_processing")).value
        assert primary.decrypt(doc["mineru_credentials"][0]["secret"].encode()) == b"mineru-key"

    # 再跑一次是空操作
    async with get_sessionmaker()() as session:
        assert await security.rotate_encrypted_secrets(session) == 0


async def test_rotation_is_a_noop_without_a_new_key(app, monkeypatch):
    """从源码跑、仍用默认 secret_key：主密钥就是旧派生密钥，没东西可换。"""
    monkeypatch.setattr(get_settings(), "secret_key", LEGACY_DEFAULT_SECRET_KEY, raising=False)
    monkeypatch.setattr(get_settings(), "encryption_key", "", raising=False)
    security.get_fernet.cache_clear()
    try:
        async with get_sessionmaker()() as session:
            session.add(
                LLMProviderConfig(
                    name="p",
                    kind="openai_compat",
                    api_key_encrypted=_legacy_fernet().encrypt(b"k").decode(),
                )
            )
            await session.commit()
        async with get_sessionmaker()() as session:
            assert await security.rotate_encrypted_secrets(session) == 0
    finally:
        security.get_fernet.cache_clear()


# ---- 浏览器扩展 key：旧摘要认一次后升级 ----


async def test_download_key_hashed_with_legacy_secret_keeps_working(client):
    token = (await client.post("/api/auth/local-session")).json()["access_token"]
    created = await client.post(
        "/api/me/download-api-key", headers={"Authorization": f"Bearer {token}"}
    )
    raw = created.json()["api_key"]
    legacy_hash = hmac.new(
        LEGACY_DEFAULT_SECRET_KEY.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()
    async with get_sessionmaker()() as session:
        key = await session.scalar(select(DownloadApiKey))
        key.secret_hash = legacy_hash
        await session.commit()

    res = await client.get("/api/download-client/me", headers={"X-Polaris-API-Key": raw})
    assert res.status_code == 200, res.text
    async with get_sessionmaker()() as session:
        key = await session.scalar(select(DownloadApiKey))
        assert key.secret_hash != legacy_hash  # 已改存当前密钥下的摘要

    bad = await client.get("/api/download-client/me", headers={"X-Polaris-API-Key": raw + "x"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "DOWNLOAD_API_KEY_INVALID"
