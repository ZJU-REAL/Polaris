"""PolarisBuddy 的陪伴层：问候语与页面上下文。

问候语的价值全在**数字是真的**上。它不过模型，所以这里能把每一句话钉死：
数字对不上就是 bug，不是"模型今天心情不好"。
"""

import os
import uuid

import pytest

from app.core.db import get_sessionmaker
from app.services import buddy
from tests.conftest import register_and_login


@pytest.fixture
def agent_on(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


def _stats(**kw) -> buddy.BuddyStats:
    base = {
        "saved_recent": 0,
        "saved_total": 0,
        "ideas_recent": 0,
        "experiments_running": 0,
        "daily_today": 0,
    }
    return buddy.BuddyStats(**{**base, **kw})


def test_greeting_reports_the_real_numbers():
    """句子里的数字必须来自计数——这是它不过模型的全部理由。"""
    assert "3 个实验" in buddy.compose_greeting(_stats(experiments_running=3))
    assert "5 个新想法" in buddy.compose_greeting(_stats(ideas_recent=5))
    assert "7 篇论文进库" in buddy.compose_greeting(_stats(saved_recent=7))
    assert "12 篇论文" in buddy.compose_greeting(_stats(daily_today=12))


def test_greeting_says_one_thing_at_a_time():
    """同时有实验和想法时只说最要紧的那件事。问候语不是仪表盘。"""
    line = buddy.compose_greeting(_stats(experiments_running=2, ideas_recent=9, saved_recent=4))
    assert "2 个实验" in line
    assert "9" not in line and "4 篇" not in line


def test_greeting_without_any_data_still_greets():
    """什么都没有时也要有一句话——新用户开面板看到空白最劝退。"""
    line = buddy.compose_greeting(_stats(), name="小明")
    assert line.startswith("小明，")
    assert "PolarisBuddy" in line


def test_nudge_stays_quiet_when_there_is_nothing_new():
    """主动提示的门槛比问候语高：没有真事就不该敲肩膀。

    为了让悬浮球看起来「活着」而常亮的红点，是在教用户忽略它。
    """
    assert buddy.compose_nudge(_stats()) is None
    # 光是「库里有存货」不算新事——它昨天也在那儿
    assert buddy.compose_nudge(_stats(saved_total=200)) is None
    assert buddy.compose_nudge(_stats(saved_recent=3)) is None


def test_nudge_speaks_only_about_what_is_actually_new():
    assert "2 个实验" in (buddy.compose_nudge(_stats(experiments_running=2)) or "")
    assert "9 篇" in (buddy.compose_nudge(_stats(daily_today=9)) or "")
    # 一次只说一件事
    line = buddy.compose_nudge(_stats(experiments_running=1, daily_today=9)) or ""
    assert "9" not in line


def test_page_context_renders_only_known_kinds():
    """认不出的 kind 返回空串：前端能声明任何字符串，不能让它随便往提示词里塞话。"""
    line = buddy.render_page_context("paper", "abc-123")
    assert "正在读一篇论文" in line and "abc-123" in line
    assert buddy.render_page_context("whatever", "x") == ""
    assert buddy.render_page_context(None, None) == ""
    # 长度封顶：它是前端声明的，不能无限长
    assert len(buddy.render_page_context("paper", "y" * 500)) <= buddy.MAX_CONTEXT_CHARS


async def test_stats_only_count_this_users_data(client):
    """别人库里的论文不算在我头上；只浏览没收藏的也不算。"""
    from app.models.library import UserLibraryEntry

    token_a = await register_and_login(client, email="buddy-a@example.com")
    token_b = await register_and_login(client, email="buddy-b@example.com")
    assert token_a and token_b

    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from app.models.user import User

        users = {
            u.email: u.id
            for u in (await session.execute(select(User))).scalars().all()
        }
        a, b = users["buddy-a@example.com"], users["buddy-b@example.com"]
        for i in range(3):
            session.add(
                UserLibraryEntry(
                    user_id=a, dedup_key=str(uuid.uuid4()), title=f"A{i}", saved=True
                )
            )
        # 只浏览没收藏的条目不算"收进库"
        session.add(
            UserLibraryEntry(user_id=a, dedup_key=str(uuid.uuid4()), title="browsed", saved=False)
        )
        session.add(
            UserLibraryEntry(user_id=b, dedup_key=str(uuid.uuid4()), title="B0", saved=True)
        )
        await session.commit()

        stats_a = await buddy.collect_stats(session, user_id=a)
        stats_b = await buddy.collect_stats(session, user_id=b)
    assert stats_a.saved_total == 3
    assert stats_b.saved_total == 1


async def test_greeting_endpoint_returns_sentence_and_counts(client, agent_on):
    token = await register_and_login(client, email="buddy-ep@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get("/api/chat/buddy/greeting", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["greeting"]
    assert "nudge" in body  # 没有真事时是 null，前端据此决定要不要冒气泡
    assert set(body["stats"]) == {
        "saved_recent",
        "saved_total",
        "ideas_recent",
        "experiments_running",
        "daily_today",
    }


async def test_page_context_reaches_the_model_as_a_user_prefix(client, agent_on, monkeypatch):
    """页面上下文拼在**本轮提问**前面，不进 system——system 是稳定前缀，
    每轮变一次等于 prompt cache 永不命中。"""
    from app.agents.chat import loop as loop_mod

    seen: list[list] = []
    original = loop_mod.ChatAgentLoop.run

    def spy(self, req):
        seen.append([req.page_context, req.question])
        return original(self, req)

    monkeypatch.setattr(loop_mod.ChatAgentLoop, "run", spy)

    token = await register_and_login(client, email="buddy-ctx@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    conv = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv['id']}/turn",
        json={"question": "这篇讲了什么", "page_kind": "paper", "page_id": "p-1"},
        headers=headers,
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass

    assert seen, "循环没被调用"
    page_context, question = seen[0]
    assert "正在读一篇论文" in page_context and "p-1" in page_context
    assert question == "这篇讲了什么", "提问本身不该被改写"
