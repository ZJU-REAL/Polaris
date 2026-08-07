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
        "reading_now": 0,
        "manuscripts_active": 0,
        "topics": 0,
    }
    return buddy.BuddyStats(**{**base, **kw})


def test_greeting_reports_the_real_numbers():
    """句子里的数字必须来自计数——这是它不过模型的全部理由。"""
    assert "3 个实验" in buddy.compose_greeting(_stats(experiments_running=3))
    assert "5 个新想法" in buddy.compose_greeting(_stats(ideas_recent=5))
    assert "7 篇论文进库" in buddy.compose_greeting(_stats(saved_recent=7))


def test_greeting_does_not_count_todays_papers():
    """有新论文的日子只是打个招呼，不报数、不许诺筛选。

    这句话以前是「今天新到了 N 篇论文，要我先替你筛一遍吗」。N 每天在变却不帮人做
    任何决定；而「替你筛一遍」一轮对话根本干不了——筛论文是每日池打分收录那条流水线
    的活。答应下来只能敷衍，不如不说。
    """
    line = buddy.compose_greeting(_stats(daily_today=384))
    assert "384" not in line
    assert "筛" not in line
    assert line  # 但仍然要有一句话


def test_opening_always_offers_exactly_three_replies():
    """三条是版面定死的——多一条挤版，少一条留个空位，两种都难看。

    每一条分支都要给满三条，包括什么都没有的新账号：空屏本来就最劝退，
    再给不出可点的东西就真的只能干瞪眼。
    """
    cases = [
        _stats(),  # 全新账号
        _stats(experiments_running=1),
        _stats(manuscripts_active=1),
        _stats(reading_now=2),
        _stats(ideas_recent=3),
        _stats(saved_recent=4),
        _stats(daily_today=5),
        _stats(saved_total=6),
    ]
    for s in cases:
        question, cards = buddy.compose_opening(s)
        assert question
        assert len(cards) == 3
        assert all(c["summary"].strip() and c["prompt"].strip() for c in cards)
    # 七种页面上下文同样都要给满
    for kind in ("paper", "idea", "experiment", "manuscript", "library", "project", "daily"):
        question, cards = buddy.compose_opening(_stats(), page_kind=kind)
        assert question
        assert len(cards) == 3


def test_opening_prefers_the_page_over_the_backlog():
    """在读一篇论文时，不该被问「你的实验跑到哪了」——他此刻不在那儿。"""
    busy = _stats(experiments_running=3, daily_today=384)
    on_paper, _ = buddy.compose_opening(busy, page_kind="paper")
    elsewhere, _ = buddy.compose_opening(busy)
    assert on_paper != elsewhere
    assert "实验" not in on_paper


def test_opening_never_reports_a_count():
    """开场问话里不出现数字：384 这种数每天都在变，却不帮人做任何决定。"""
    question, cards = buddy.compose_opening(_stats(daily_today=384, saved_recent=12))
    for text in [question, *(c["summary"] for c in cards), *(c["prompt"] for c in cards)]:
        assert not any(ch.isdigit() for ch in text), text


def test_card_summary_is_short_and_prompt_is_a_whole_question():
    """卡面给眼睛扫，问题给模型读——两者混为一谈就会有一头出问题。

    摘要长了在卡上折行；问题短了进输入框就是个没头没尾的词组，模型只能瞎猜。
    """
    for kind in (None, "paper", "idea", "experiment", "manuscript", "library", "project", "daily"):
        _, cards = buddy.compose_opening(_stats(), page_kind=kind)
        for card in cards:
            assert len(card["summary"]) <= 8, card
            # 完整的问句：说得出上下文，且以问号或句号收尾
            assert len(card["prompt"]) >= 15, card
            assert card["prompt"][-1] in "？。", card
            assert card["prompt"] != card["summary"]


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
        "reading_now",
        "manuscripts_active",
        "topics",
    }
    # 开场：一句问话 + 三张卡片（卡面摘要，点开是完整问题）
    assert body["question"]
    assert len(body["cards"]) == 3
    assert all(c["summary"] and c["prompt"] for c in body["cards"])


async def test_opening_follows_the_page_the_user_is_on(client, agent_on):
    """用户此刻在看什么，是最强的信号——他人就在那一页上。"""
    token = await register_and_login(client, email="buddy-page@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    cold = (await client.get("/api/chat/buddy/greeting", headers=headers)).json()
    on_paper = (
        await client.get("/api/chat/buddy/greeting?page=paper", headers=headers)
    ).json()
    assert on_paper["question"] != cold["question"]
    assert len(on_paper["cards"]) == 3

    # 认不出的 kind 不该把开场搞没，退回近况那条路
    junk = (await client.get("/api/chat/buddy/greeting?page=nonsense", headers=headers)).json()
    assert junk["question"] == cold["question"]


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


async def test_memories_are_per_user_and_reach_the_prompt(client, agent_on):
    """长期记忆：只有自己看得见，并且真的进了这一轮的提示词。"""
    from app.core.db import get_sessionmaker
    from app.services import buddy as buddy_service

    token_a = await register_and_login(client, email="mem-a@example.com")
    token_b = await register_and_login(client, email="mem-b@example.com")
    mine = {"Authorization": f"Bearer {token_a}"}
    theirs = {"Authorization": f"Bearer {token_b}"}

    created = await client.post(
        "/api/chat/memories", json={"text": "我做具身智能，别给我推纯 NLP"}, headers=mine
    )
    assert created.status_code == 201

    assert len((await client.get("/api/chat/memories", headers=mine)).json()) == 1
    assert (await client.get("/api/chat/memories", headers=theirs)).json() == []

    # 别人的记忆删不掉（404 而不是 403：403 等于承认这条存在）
    memory_id = created.json()["id"]
    assert (
        await client.delete(f"/api/chat/memories/{memory_id}", headers=theirs)
    ).status_code == 404

    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from app.models.user import User

        user_id = (
            await session.execute(select(User.id).where(User.email == "mem-a@example.com"))
        ).scalar_one()
        rendered = await buddy_service.render_memories(session, user_id=user_id)
    assert "具身智能" in rendered


async def test_memory_injection_is_capped():
    """记忆每轮都要重发：不封顶就是每轮都在为一堆旧便签付钱。"""
    from app.services.buddy import MAX_MEMORY_CHARS

    assert MAX_MEMORY_CHARS <= 2000


async def test_a_user_with_no_memories_adds_nothing_to_the_prompt(client, agent_on):
    """没有记忆时一个字都不加——空标题会白占提示词，还会让模型以为该找点什么。"""
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.services import buddy as buddy_service

    assert await register_and_login(client, email="mem-empty@example.com")
    async with get_sessionmaker()() as session:
        assert await buddy_service.render_memories(session, user_id=_uuid.uuid4()) == ""
