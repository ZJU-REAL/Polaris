"""#737 配置分层：用户偏好存 owner 用户的 settings。旧 system_settings 行的回退已删（#821 E2）。"""

from sqlalchemy import select

from app.api.auth import LOCAL_USER_EMAIL
from app.models.system_setting import SystemSetting
from app.models.user import User
from tests.conftest import register_and_login


async def _owner_settings_snapshot(session) -> dict:
    from app.services import owner_settings

    owner = await owner_settings.owner_user(session)
    assert owner is not None
    return dict(owner.settings or {})


async def test_owner_is_earliest_active_user(client):
    """偏好落在最早的活跃用户上——单机就是本地用户。

    老安装库里可能还留着更晚的别的用户行（以前注册过的），直接入库模拟。
    """
    import datetime as dt

    from fastapi_users.password import PasswordHelper

    from app.core.db import get_sessionmaker
    from app.services import owner_settings
    from app.services.owner import reset_owner_cache

    await register_and_login(client)
    async with get_sessionmaker()() as session:
        local = await session.scalar(select(User).where(User.email == LOCAL_USER_EMAIL))
        session.add(
            User(
                email="later@e.com",
                hashed_password=PasswordHelper().hash("x"),
                is_active=True,
                is_superuser=False,
                is_verified=True,
                display_name="Later",
                username="later",
                created_at=local.created_at + dt.timedelta(days=1),
            )
        )
        await session.commit()

        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == LOCAL_USER_EMAIL

        # 与 #722 的 owner 守卫共用同一事实源：进程内缓存，首用户失活也不换主……
        local.is_active = False
        await session.commit()
        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == LOCAL_USER_EMAIL

        # ……重启进程（这里用 reset 模拟）后顺延到下一位活跃用户
        reset_owner_cache()
        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == "later@e.com"


async def test_daily_preferences_live_on_the_owner(client):
    """每日偏好落在 owner 的 settings 上；非 owner 被 #722 的守卫挡在 API 层。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    owner_headers = {"Authorization": f"Bearer {await register_and_login(client)}"}

    resp = await client.put(
        "/api/daily/categories", json={"categories": ["stat.ML"]}, headers=owner_headers
    )
    assert resp.status_code == 200
    resp = await client.put("/api/daily/retention", json={"days": 30}, headers=owner_headers)
    assert resp.status_code == 200
    resp = await client.put(
        "/api/daily/sync-time", json={"hour": 7, "minute": 15}, headers=owner_headers
    )
    assert resp.status_code == 200
    resp = await client.put("/api/daily/sync-scope", json={"scope": "full"}, headers=owner_headers)
    assert resp.status_code == 200

    async with get_sessionmaker()() as session:
        stored = await _owner_settings_snapshot(session)
        assert stored["daily.retention_days"] == 30
        assert stored["daily.sync_time"] == "07:15"
        assert stored["daily.sync_scope"] == "full"
        # 新写入不再落 system_settings
        for key in (
            "daily_feed_categories",
            "daily_feed_retention_days",
            "daily_feed_sync_time",
            "library_sync_scope",
        ):
            assert await session.get(SystemSetting, key) is None
        # 读路径（worker 同款：只有 session）取到同一份真相
        assert await daily_feed.get_retention_days(session) == 30
        assert await daily_feed.get_sync_time(session) == (7, 15)
        assert await daily_feed.get_sync_scope(session) == "full"


async def test_legacy_system_settings_rows_are_no_longer_read(client):
    """回退只说好留一期（#737），#821 E2 删了：旧行即使还在也不再被读到。

    迁移 d3f9a1c7e2b4 已先把旧行里 owner 还没有的值补到 owner 身上再删行，所以正常
    部署上根本不会剩下旧行；这里验证的是读路径确实只认 owner.settings。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed, tts
    from app.services.affiliations import get_affiliation_extraction_mode

    await register_and_login(client)
    async with get_sessionmaker()() as session:
        session.add(SystemSetting(key="daily_feed_retention_days", value=9))
        session.add(SystemSetting(key="affiliation_extraction_mode", value="on_compile"))
        session.add(SystemSetting(key="tts_config", value={"enabled": True, "model": "legacy"}))
        await session.commit()

        assert await daily_feed.get_retention_days(session) != 9
        assert await get_affiliation_extraction_mode(session) == "on_add"
        assert (await tts.get_admin_settings(session)).get("model") != "legacy"

        await daily_feed.set_retention_days(session, 21)
        assert await daily_feed.get_retention_days(session) == 21


async def test_writing_a_preference_with_no_user_at_all_says_so(app):
    """以前退回写 system_settings 旧行；读路径不再读它之后那样写就是写丢，所以明说。

    实际不会走到：所有写入都来自已登录用户的请求。
    """
    import pytest

    from app.core.db import get_sessionmaker
    from app.services import daily_feed
    from app.services.owner_settings import NoOwnerError

    async with get_sessionmaker()() as session:
        with pytest.raises(NoOwnerError):
            await daily_feed.set_retention_days(session, 12)


async def test_tts_and_affiliation_settings_live_on_the_owner(client):
    from app.core.db import get_sessionmaker
    from app.services import tts

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    resp = await client.put(
        "/api/admin/settings/tts",
        json={
            "enabled": True,
            "provider": "openai_compatible",
            "base_url": "http://tts.local:5000/v1",
            "model": "my-voice-model",
            "default_voice": "default",
            "default_speed": 1.0,
            "max_chars": 20000,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.put(
        "/api/admin/settings/affiliation-mode", json={"mode": "on_compile"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    async with get_sessionmaker()() as session:
        stored = await _owner_settings_snapshot(session)
        assert stored["tts.admin"]["model"] == "my-voice-model"
        assert stored["affiliations.extraction_mode"] == "on_compile"
        assert await session.get(SystemSetting, "tts_config") is None
        assert await session.get(SystemSetting, "affiliation_extraction_mode") is None
        # 个人 TTS 偏好与全局档互不覆盖（同住一个 settings 字典的不同键）
        _, effective = await tts.effective_settings(
            session,
            await session.scalar(select(User).where(User.email == LOCAL_USER_EMAIL)),
        )
        assert effective["effective_model"] == "my-voice-model"
