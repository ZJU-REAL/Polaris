"""#737 配置分层：用户偏好存 owner 用户的 settings，旧 system_settings 键只读回退。"""

from sqlalchemy import select

from app.models.system_setting import SystemSetting
from app.models.user import User
from tests.conftest import register_and_login


async def _owner_settings_snapshot(session) -> dict:
    from app.services import owner_settings

    owner = await owner_settings.owner_user(session)
    assert owner is not None
    return dict(owner.settings or {})


async def test_owner_is_earliest_active_user(client):
    from app.core.db import get_sessionmaker
    from app.services import owner_settings
    from app.services.owner import reset_owner_cache

    await register_and_login(client, email="first@e.com")
    await register_and_login(client, email="second@e.com")
    async with get_sessionmaker()() as session:
        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == "first@e.com"

        # 与 #722 的 owner 守卫共用同一事实源：进程内缓存，首用户失活也不换主……
        first = await session.scalar(select(User).where(User.email == "first@e.com"))
        first.is_active = False
        await session.commit()
        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == "first@e.com"

        # ……重启进程（这里用 reset 模拟）后顺延到下一位活跃用户
        reset_owner_cache()
        owner = await owner_settings.owner_user(session)
        assert owner is not None and owner.email == "second@e.com"


async def test_daily_preferences_live_on_the_owner(client):
    """每日偏好落在 owner 的 settings 上；非 owner 被 #722 的守卫挡在 API 层。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    owner_headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    other_headers = {
        "Authorization": f"Bearer {await register_and_login(client, email='u2@e.com')}"
    }

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
    resp = await client.put(
        "/api/daily/sync-scope", json={"scope": "full"}, headers=owner_headers
    )
    assert resp.status_code == 200
    # 非 owner：读得到，写被 403（#722）
    resp = await client.get("/api/daily/categories", headers=other_headers)
    assert resp.status_code == 200 and resp.json()["categories"] == ["stat.ML"]
    resp = await client.put(
        "/api/daily/retention", json={"days": 3}, headers=other_headers
    )
    assert resp.status_code == 403

    async with get_sessionmaker()() as session:
        stored = await _owner_settings_snapshot(session)
        assert stored["daily.categories"] == ["stat.ML"]
        assert stored["daily.retention_days"] == 30
        assert stored["daily.sync_time"] == "07:15"
        assert stored["daily.sync_scope"] == "full"
        # 非 owner 用户的 settings 不被写
        other = await session.scalar(select(User).where(User.email == "u2@e.com"))
        assert not (other.settings or {})
        # 新写入不再落 system_settings
        for key in (
            daily_feed.CATEGORIES_SETTING_KEY,
            daily_feed.RETENTION_SETTING_KEY,
            daily_feed.SYNC_TIME_SETTING_KEY,
            daily_feed.SYNC_SCOPE_SETTING_KEY,
        ):
            assert await session.get(SystemSetting, key) is None
        # 读路径（worker 同款：只有 session）取到同一份真相
        assert await daily_feed.get_categories(session) == ["stat.ML"]
        assert await daily_feed.get_retention_days(session) == 30
        assert await daily_feed.get_sync_time(session) == (7, 15)
        assert await daily_feed.get_sync_scope(session) == "full"


async def test_legacy_system_settings_rows_are_read_as_fallback(client):
    """迁移期回退：新键缺席时读旧 system_settings 行；写过新键后旧行失效。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed, tts
    from app.services.affiliations import get_affiliation_extraction_mode

    await register_and_login(client)
    async with get_sessionmaker()() as session:
        session.add(SystemSetting(key=daily_feed.CATEGORIES_SETTING_KEY, value=["q-bio.NC"]))
        session.add(SystemSetting(key=daily_feed.RETENTION_SETTING_KEY, value=9))
        session.add(SystemSetting(key="affiliation_extraction_mode", value="on_compile"))
        session.add(
            SystemSetting(
                key=tts.SETTING_KEY, value={"enabled": True, "model": "legacy-model"}
            )
        )
        await session.commit()

        assert await daily_feed.get_categories(session) == ["q-bio.NC"]
        assert await daily_feed.get_retention_days(session) == 9
        assert await get_affiliation_extraction_mode(session) == "on_compile"
        assert (await tts.get_admin_settings(session))["model"] == "legacy-model"

        # 写新值 → 存到 owner，旧行原样保留但不再被读到
        await daily_feed.set_categories(session, ["cs.CV"])
        assert await daily_feed.get_categories(session) == ["cs.CV"]
        legacy = await session.get(SystemSetting, daily_feed.CATEGORIES_SETTING_KEY)
        assert legacy is not None and legacy.value == ["q-bio.NC"]


async def test_writes_without_any_user_fall_back_to_legacy_rows(app):
    """还没有任何用户（种子/引导阶段）：写退回旧行，读也能读回来，不丢数据。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    async with get_sessionmaker()() as session:
        await daily_feed.set_categories(session, ["stat.ML"])
        assert await daily_feed.get_categories(session) == ["stat.ML"]
        row = await session.get(SystemSetting, daily_feed.CATEGORIES_SETTING_KEY)
        assert row is not None and row.value == ["stat.ML"]


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
        assert await session.get(SystemSetting, tts.SETTING_KEY) is None
        assert await session.get(SystemSetting, "affiliation_extraction_mode") is None
        # 个人 TTS 偏好与全局档互不覆盖（同住一个 settings 字典的不同键）
        _, effective = await tts.effective_settings(
            session,
            await session.scalar(select(User).where(User.email == "alice@example.com")),
        )
        assert effective["effective_model"] == "my-voice-model"
