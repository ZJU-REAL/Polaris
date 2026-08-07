"""浏览统计：去重、可见性、只出计数。

这些口径决定了热度数字能不能当依据，所以逐条钉住。
"""

import datetime as dt
import uuid

from app.core.db import get_sessionmaker
from app.services import view_stats
from tests.conftest import register_and_login


async def _user_id(email: str):
    from sqlalchemy import select

    from app.models.user import User

    async with get_sessionmaker()() as session:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_one_person_clicking_repeatedly_counts_once(client):
    """同一个人一小时内点多少次都只算一次。

    不去重的话，热度榜衡量的是「谁刷新得最勤」——一个人来回切标签页就能屠榜。
    """
    assert await register_and_login(client, email="views-a@example.com")
    uid = await _user_id("views-a@example.com")
    target = uuid.uuid4()

    async with get_sessionmaker()() as session:
        assert await view_stats.record_view(
            session, kind="paper", target_id=target, user_id=uid
        ) is True
        for _ in range(4):
            assert await view_stats.record_view(
                session, kind="paper", target_id=target, user_id=uid
            ) is False


async def test_different_people_each_count(client):
    """去重是按人算的：两个人各看一次就是两次。"""
    assert await register_and_login(client, email="views-b@example.com")
    assert await register_and_login(client, email="views-c@example.com")
    b, c = await _user_id("views-b@example.com"), await _user_id("views-c@example.com")
    target = uuid.uuid4()

    async with get_sessionmaker()() as session:
        assert await view_stats.record_view(session, kind="paper", target_id=target, user_id=b)
        assert await view_stats.record_view(session, kind="paper", target_id=target, user_id=c)


async def test_the_window_is_an_hour(client):
    """窗口过去之后再点就算新的一次。"""
    from app.models.view_event import ViewEvent

    assert await register_and_login(client, email="views-d@example.com")
    uid = await _user_id("views-d@example.com")
    target = uuid.uuid4()

    async with get_sessionmaker()() as session:
        # 造一条一小时零一分钟前的记录
        old = ViewEvent(kind="paper", target_id=target, user_id=uid)
        old.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(
            minutes=view_stats.DEDUP_MINUTES + 1
        )
        session.add(old)
        await session.commit()

        assert await view_stats.record_view(
            session, kind="paper", target_id=target, user_id=uid
        ) is True


async def test_unknown_kinds_are_refused(client):
    """类型不做白名单的话，前端写错一个字就会在表里堆出谁也看不懂的数据。"""
    import pytest

    assert await register_and_login(client, email="views-e@example.com")
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError):
            await view_stats.record_view(
                session, kind="idea", target_id=uuid.uuid4(), user_id=None
            )


async def test_hot_lists_only_show_what_you_can_see(client):
    """看不见的库不出现在榜上，哪怕它更热。"""
    from sqlalchemy import select

    from app.models.library_direction import DirectionLibrary
    from app.models.user import User
    from app.models.view_event import ViewEvent

    # 第一个注册的是管理员，而管理员本来就看得见全部库——这条要验的是**普通成员**
    assert await register_and_login(client, email="views-admin@example.com")
    assert await register_and_login(client, email="views-f@example.com")
    uid = await _user_id("views-f@example.com")

    async with get_sessionmaker()() as session:
        public = DirectionLibrary(name="公共库", definition={"statement": "x"}, is_public=True)
        private = DirectionLibrary(name="别人的私库", definition={"statement": "x"})
        session.add_all([public, private])
        await session.flush()
        # 私库热度更高：三次 vs 一次。user_id 留空即可——去重只在有用户时生效，
        # 这里要的是「计数」而不是「谁看的」。
        for _ in range(3):
            session.add(ViewEvent(kind="library", target_id=private.id, user_id=None))
        session.add(ViewEvent(kind="library", target_id=public.id, user_id=uid))
        await session.commit()

        user = (await session.execute(select(User).where(User.id == uid))).scalar_one()
        hot = await view_stats.hot_libraries(session, user=user)

    names = [row["name"] for row in hot]
    assert "公共库" in names
    assert "别人的私库" not in names, "热度再高也不能让人看见他无权看的库"


async def test_the_endpoint_answers_with_counts_only(client):
    """API 只出总数，不按人展开——谁在读什么是敏感信息。"""
    token = await register_and_login(client, email="views-g@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    body = (await client.get("/api/lab/hot", headers=headers)).json()
    assert set(body) == {"libraries", "papers"}
    for row in body["libraries"] + body["papers"]:
        assert set(row) <= {"id", "name", "title", "views"}
