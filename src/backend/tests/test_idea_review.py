"""Idea 评审锦标赛 / 讨论 / 晋级闸门测试（fake LLM 全离线，直接驱动 VoyageEngine）。

覆盖：辩论落库与 WS 事件、Elo 更新与战绩、leaderboard、讨论区惰性创建 +
human 消息注入辩论上下文、promote → gate → approve 联动、权限。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.idea import Idea
from app.models.review import ReviewMessage, ReviewSession
from app.models.voyage import VoyageRun
from tests.conftest import RecordingBus, register_and_login

DEFAULT_PERSONA_NAMES = ["严谨方法论者", "务实工程师", "领域怀疑论者"]


async def _setup_project(client, email="alice@example.com", name="review-proj"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


_last_seeded_at: datetime | None = None


def _next_seed_time() -> datetime:
    """造数据用的创建时间：严格递增，不跟着系统时钟往回跳。

    配对在 Elo 相同时按创建时间排（review.pair），这里的用例都默认「先造的排前面」。
    系统时钟会往回拨（这台机器的容器里每十来秒跳一次 ±1 秒）：四个想法造到一半时
    跳一下，后造的反而排到前面，配出来的对局就不是用例设想的那几对了。
    """
    global _last_seeded_at
    now = utcnow()
    if _last_seeded_at is not None and now <= _last_seeded_at:
        now = _last_seeded_at + timedelta(microseconds=1)
    _last_seeded_at = now
    return now


async def _seed_idea(project_id: str, title: str, *, status="candidate") -> str:
    async with get_sessionmaker()() as session:
        idea = Idea(
            created_at=_next_seed_time(),
            project_id=uuid.UUID(project_id),
            title=title,
            summary=f"{title} 的一句话概述",
            content=f"## 动机\n\n{title} 的动机\n\n## 方法概述\n\n（略）",
            status=status,
        )
        session.add(idea)
        await session.commit()
        return str(idea.id)


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


class _FailOnePairProvider(FakeProvider):
    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, **kwargs):
        if any("FAIL_PAIR" in message.content for message in messages):
            raise RuntimeError("temporary upstream failure")
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, **kwargs
        )


async def test_tournament_fans_out_per_match_nodes(client, queue_stub):
    """4 个 idea → 2 场对局：pair 展开 2 个 review.match 节点（每场可见、可逐场查预算），
    顺序 pair → match×2 → summarize，全部通过（docs/task-system.md §7（原 voyage-loop.md §7））。"""
    project_id, headers = await _setup_project(client)
    for i in range(4):
        await _seed_idea(project_id, f"想法{i}：方向 {i}")

    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]

    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "done", detail
    actions = [s["action"] for s in detail["steps"]]
    assert actions == ["review.pair", "review.match", "review.match", "review.summarize"]
    assert all(s["status"] == "passed" for s in detail["steps"])
    # 两场对局是相邻两个节点，match_index 递增
    match_steps = [s for s in detail["steps"] if s["action"] == "review.match"]
    assert [s["observation"]["match_index"] for s in match_steps] == [0, 1]


async def test_tournament_excludes_trashed_ideas(client, queue_stub):
    project_id, headers = await _setup_project(client, name="exclude-trashed")
    visible_ids = [await _seed_idea(project_id, f"当前想法{i}") for i in range(4)]
    trashed_id = await _seed_idea(project_id, "回收站想法")
    resp = await client.delete(f"/api/ideas/{trashed_id}", headers=headers)
    assert resp.status_code == 204, resp.text

    # Explicit selection must not provide a way to re-introduce a trashed idea.
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament",
        json={"idea_ids": [visible_ids[0], trashed_id], "rounds": 1},
        headers=headers,
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "INVALID_IDEA_IDS"

    # Automatic selection sees only the four current ideas, hence two matches.
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))
    detail = (await client.get(f"/api/voyages/{resp.json()['id']}", headers=headers)).json()
    pair_observation = next(
        s["observation"] for s in detail["steps"] if s["action"] == "review.pair"
    )
    assert pair_observation["participants"] == 4
    assert pair_observation["pairs"] == 2


async def test_partial_tournament_retries_only_failed_matches(client, queue_stub):
    project_id, headers = await _setup_project(client, name="failed-match-retry")
    idea_ids = [
        await _seed_idea(project_id, "想法 A"),
        await _seed_idea(project_id, "想法 B"),
        await _seed_idea(project_id, "FAIL_PAIR 想法 C"),
        await _seed_idea(project_id, "FAIL_PAIR 想法 D"),
    ]
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    router = LLMRouter()
    router.override_provider(_FailOnePairProvider())
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(
        uuid.UUID(resp.json()["id"])
    )

    summary = (
        await client.get(f"/api/projects/{project_id}/review/tournament/latest", headers=headers)
    ).json()
    assert summary["planned"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["can_retry"] is True

    retry = await client.post(
        f"/api/projects/{project_id}/review/tournament/retry-failed", headers=headers
    )
    assert retry.status_code == 201, retry.text
    duplicate = await client.post(
        f"/api/projects/{project_id}/review/tournament/retry-failed", headers=headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"

    retry_engine, _ = _make_engine()
    await retry_engine.run(uuid.UUID(retry.json()["id"]))
    summary = (
        await client.get(f"/api/projects/{project_id}/review/tournament/latest", headers=headers)
    ).json()
    assert summary["is_retry"] is True
    assert summary["planned"] == 1
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert summary["can_retry"] is False

    async with get_sessionmaker()() as session:
        ideas = [await session.get(Idea, uuid.UUID(idea_id)) for idea_id in idea_ids]
        assert all(idea is not None and idea.matches == 1 for idea in ideas)


async def test_retry_is_the_latest_tournament_even_when_the_clock_stepped_back(client, queue_stub):
    """补赛建在系统时钟往回拨之后：它的创建时间比原锦标赛还早，但它仍然是「最新一届」。

    以前只按创建时间取最新：这种情况下取回的是原锦标赛——汇总里还是那场失败的对局、
    看不出已经补过；再点一次「补赛」会把补赛已经打完的那场重打一遍，战绩多记一场。
    """
    project_id, headers = await _setup_project(client, name="retry-clock-step")
    idea_ids = [
        await _seed_idea(project_id, "想法 A"),
        await _seed_idea(project_id, "想法 B"),
        await _seed_idea(project_id, "FAIL_PAIR 想法 C"),
        await _seed_idea(project_id, "FAIL_PAIR 想法 D"),
    ]
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["id"]
    router = LLMRouter()
    router.override_provider(_FailOnePairProvider())
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(uuid.UUID(source_id))

    retry = await client.post(
        f"/api/projects/{project_id}/review/tournament/retry-failed", headers=headers
    )
    assert retry.status_code == 201, retry.text
    retry_id = retry.json()["id"]
    # 时钟在两次创建之间往回拨了一秒：补赛的创建时间落在原锦标赛之前
    async with get_sessionmaker()() as session:
        source_run = await session.get(VoyageRun, uuid.UUID(source_id))
        retry_run = await session.get(VoyageRun, uuid.UUID(retry_id))
        retry_run.created_at = source_run.created_at - timedelta(seconds=1)
        await session.commit()

    latest_url = f"/api/projects/{project_id}/review/tournament/latest"
    summary = (await client.get(latest_url, headers=headers)).json()
    assert summary["voyage_id"] == retry_id
    assert summary["is_retry"] is True
    assert summary["root_voyage_id"] == source_id

    retry_engine, _ = _make_engine()
    await retry_engine.run(uuid.UUID(retry_id))
    summary = (await client.get(latest_url, headers=headers)).json()
    assert summary["voyage_id"] == retry_id
    assert (summary["planned"], summary["completed"], summary["failed"]) == (1, 1, 0)
    assert summary["can_retry"] is False

    # 补赛已经把那场补上了：不能再从原锦标赛起一届补赛把它重打一遍
    again = await client.post(
        f"/api/projects/{project_id}/review/tournament/retry-failed", headers=headers
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "TOURNAMENT_RETRY_UNAVAILABLE"
    async with get_sessionmaker()() as session:
        ideas = [await session.get(Idea, uuid.UUID(idea_id)) for idea_id in idea_ids]
        assert [idea.matches for idea in ideas] == [1, 1, 1, 1]


def test_seed_times_keep_increasing_when_the_clock_steps_back(monkeypatch):
    """造数据的创建时间不跟系统时钟往回走：时钟拨回一秒，后造的仍然排在后面。"""
    import tests.test_idea_review as this_module

    before = _next_seed_time()
    with monkeypatch.context() as patched:
        patched.setattr(this_module, "utcnow", lambda: before - timedelta(seconds=1))
        during = [_next_seed_time() for _ in range(3)]
    after = _next_seed_time()
    times = [before, *during, after]
    assert times == sorted(set(times))


async def test_failed_match_retry_rejects_trashed_participant(client, queue_stub):
    project_id, headers = await _setup_project(client, name="retry-trashed")
    await _seed_idea(project_id, "想法 A")
    await _seed_idea(project_id, "想法 B")
    failed_a = await _seed_idea(project_id, "FAIL_PAIR 想法 C")
    await _seed_idea(project_id, "FAIL_PAIR 想法 D")
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    router = LLMRouter()
    router.override_provider(_FailOnePairProvider())
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(
        uuid.UUID(resp.json()["id"])
    )
    trashed = await client.delete(f"/api/ideas/{failed_a}", headers=headers)
    assert trashed.status_code == 204, trashed.text

    retry = await client.post(
        f"/api/projects/{project_id}/review/tournament/retry-failed", headers=headers
    )
    assert retry.status_code == 409
    assert retry.json()["detail"] == "TOURNAMENT_RETRY_UNAVAILABLE"


async def test_leaderboard_ranks_are_honest_about_ties(client, queue_stub):
    """一轮比较里每个想法只对一个对手：四个想法比完是两个 1216、两个 1184。
    排行榜必须说「并列第 1」「并列第 3」，而不是按列表位置编出 1、2、3、4；
    没比过的想法没有名次。再跑一轮（按评分相邻配对）才把首尾的并列拆开。"""
    project_id, headers = await _setup_project(client)
    for n in range(4):
        await _seed_idea(project_id, f"候选想法 {n}")
    bystander = await _seed_idea(project_id, "没参加比较的想法", status="promoted")

    async def _run_round() -> list[dict]:
        resp = await client.post(
            f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
        )
        assert resp.status_code == 201, resp.text
        engine, _ = _make_engine()
        await engine.run(uuid.UUID(resp.json()["id"]))
        resp = await client.get(f"/api/projects/{project_id}/review/leaderboard", headers=headers)
        return resp.json()

    board = await _run_round()
    played = [e for e in board if e["matches"] > 0]
    assert [e["elo_rating"] for e in played] == [1216.0, 1216.0, 1184.0, 1184.0]
    assert [(e["rank"], e["rank_shared_by"], e["tied"]) for e in played] == [
        (1, 2, True),
        (1, 2, True),
        (3, 2, True),
        (3, 2, True),
    ]
    idle = next(e for e in board if e["id"] == bystander)
    assert idle["matches"] == 0
    assert (idle["rank"], idle["rank_shared_by"], idle["tied"]) == (None, 0, False)
    # 列表顺序和原有字段不变：仍按评分降序
    assert [e["elo_rating"] for e in board] == sorted(
        (e["elo_rating"] for e in board), reverse=True
    )

    board = await _run_round()
    played = [e for e in board if e["matches"] > 0]
    assert all(e["matches"] == 2 for e in played)
    assert [e["elo_rating"] for e in played] == [1232.0, 1200.0, 1200.0, 1168.0]
    assert [(e["rank"], e["rank_shared_by"], e["tied"]) for e in played] == [
        (1, 1, False),
        (2, 2, True),
        (2, 2, True),
        (4, 1, False),
    ]


async def test_tournament_debate_elo_and_leaderboard(client, queue_stub):
    project_id, headers = await _setup_project(client)
    idea_a = await _seed_idea(project_id, "想法甲：agent 规划新范式")
    idea_b = await _seed_idea(project_id, "想法乙：评测基准重构")

    # 参与者不足 → 400
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament",
        json={"idea_ids": [idea_a]},
        headers=headers,
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "NOT_ENOUGH_IDEAS"
    # 未知 idea id → 400
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament",
        json={"idea_ids": [idea_a, str(uuid.uuid4())]},
        headers=headers,
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "INVALID_IDEA_IDS"

    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "idea_review"
    run_id = voyage["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs

    # forge 互斥：review 在跑时 forge → 409
    resp = await client.post(f"/api/projects/{project_id}/forge", json={}, headers=headers)
    assert resp.status_code == 409

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    # 配对 → 单场辩论（review.match，由 pair 信号展开）→ 汇总
    assert [s["action"] for s in detail["steps"]] == [
        "review.pair",
        "review.match",
        "review.summarize",
    ]
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 3
    assert detail["steps"][0]["observation"]["pairs"] == 1
    assert detail["steps"][1]["observation"]["winner"] in ("a", "b")
    # 展开留痕：plan_history 记一次 fan-out（source=signal）
    assert any(e["source"] == "signal" and "辩论" in e["reason"] for e in detail["plan_history"])

    async with get_sessionmaker()() as session:
        match = (
            (
                await session.execute(
                    select(ReviewSession).where(ReviewSession.target_type == "idea_match")
                )
            )
            .scalars()
            .one()
        )
        # 同 elo 按创建时间排序：先建的想法甲为正方（idea_a）；fake 裁判判 a 胜
        assert match.target_id == uuid.UUID(idea_a)
        assert match.payload["idea_a"] == idea_a and match.payload["idea_b"] == idea_b
        assert match.payload["winner"] == "a" and match.payload["reason"]
        assert match.status == "closed"

        messages = (
            (
                await session.execute(
                    select(ReviewMessage)
                    .where(ReviewMessage.session_id == match.id)
                    .order_by(ReviewMessage.round)
                )
            )
            .scalars()
            .all()
        )
        # rounds=1：正方 + 反方 + 裁判 = 3 条，round 递增，默认人设名
        assert [m.round for m in messages] == [1, 2, 3]
        assert [m.author_name for m in messages] == DEFAULT_PERSONA_NAMES
        assert all(m.author_type == "agent" for m in messages)
        assert "判定胜者：a" in messages[-1].content

        a = await session.get(Idea, uuid.UUID(idea_a))
        b = await session.get(Idea, uuid.UUID(idea_b))
        # Elo K=32、双方初始 1200：胜者 +16 / 负者 -16
        assert a.elo_rating == 1216.0 and b.elo_rating == 1184.0
        assert a.matches == 1 and b.matches == 1
        assert a.wins == 1 and b.wins == 0
        assert a.status == "under_review" and b.status == "under_review"

    # WS 事件：参与 idea 置 under_review + 每条辩论消息
    notify_types = [m["type"] for _, m in bus.notify]
    assert notify_types.count("idea.status") == 2
    review_events = [m for _, m in bus.notify if m["type"] == "review.message"]
    assert len(review_events) == 3
    assert review_events[0]["session_id"] and review_events[0]["project_id"] == project_id
    assert review_events[0]["message"]["author_name"] == DEFAULT_PERSONA_NAMES[0]

    # leaderboard：elo 降序 + matches/wins
    resp = await client.get(f"/api/projects/{project_id}/review/leaderboard", headers=headers)
    board = resp.json()
    assert [entry["id"] for entry in board] == [idea_a, idea_b]
    assert board[0]["elo_rating"] == 1216.0
    assert board[0]["matches"] == 1 and board[0]["wins"] == 1
    assert board[1]["wins"] == 0
    assert [(e["rank"], e["tied"]) for e in board] == [(1, False), (2, False)]

    # 两个 idea 的 sessions 都能看到这场辩论（idea_b 经 payload 关联）+ 惰性讨论区
    for idea_id in (idea_a, idea_b):
        resp = await client.get(f"/api/ideas/{idea_id}/sessions", headers=headers)
        sessions = resp.json()
        types = sorted(s["target_type"] for s in sessions)
        assert types == ["idea_discussion", "idea_match"]

    # 消息 API
    match_id = next(s["id"] for s in sessions if s["target_type"] == "idea_match")
    resp = await client.get(f"/api/sessions/{match_id}/messages", headers=headers)
    api_messages = resp.json()
    assert len(api_messages) == 3
    assert set(api_messages[0]) == {
        "id",
        "session_id",
        "author_type",
        "author_name",
        "content",
        "round",
        "created_at",
    }


class _RecordingProvider(FakeProvider):
    """记录全部 prompt 的 fake provider（断言上下文注入用）。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None):
        self.prompts.append("\n".join(m.content for m in messages))
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )


async def test_discussion_and_human_context_injection(client, queue_stub, bus_recorder):
    project_id, headers = await _setup_project(client)
    idea_a = await _seed_idea(project_id, "想法甲：agent 规划新范式")
    await _seed_idea(project_id, "想法乙：评测基准重构")

    # 惰性创建讨论区（幂等）
    resp = await client.get(f"/api/ideas/{idea_a}/sessions", headers=headers)
    sessions = resp.json()
    assert [s["target_type"] for s in sessions] == ["idea_discussion"]
    discussion_id = sessions[0]["id"]
    resp = await client.get(f"/api/ideas/{idea_a}/sessions", headers=headers)
    assert [s["id"] for s in resp.json()] == [discussion_id]  # 不重复创建

    # human 消息：落库 + WS review.message
    comment = "人类评论：baseline 选择存疑，请对比 ReAct"
    resp = await client.post(
        f"/api/sessions/{discussion_id}/messages", json={"content": comment}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    message = resp.json()
    assert message["author_type"] == "human"
    assert message["author_name"] == "Local"
    assert message["round"] == 1
    ws_messages = [m for _, m in bus_recorder.notify if m["type"] == "review.message"]
    assert len(ws_messages) == 1
    assert ws_messages[0]["session_id"] == discussion_id
    assert ws_messages[0]["project_id"] == project_id
    assert ws_messages[0]["message"]["content"] == comment

    resp = await client.get(f"/api/sessions/{discussion_id}/messages", headers=headers)
    assert [m["content"] for m in resp.json()] == [comment]

    # 锦标赛：human 评论注入辩论 agent 上下文（fake LLM 收到的 prompt 含该评论）
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    run_id = resp.json()["id"]
    provider = _RecordingProvider()
    router = LLMRouter()
    router.override_provider(provider)
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    debate_prompts = [p for p in provider.prompts if "人类评审意见" in p]
    assert debate_prompts, "辩论 prompt 应包含人类评审意见区块"
    assert any(comment in p and "Local" in p for p in debate_prompts)


async def test_custom_personas_used_in_messages(client, queue_stub):
    project_id, headers = await _setup_project(client)
    idea_a = await _seed_idea(project_id, "想法甲")
    await _seed_idea(project_id, "想法乙")
    personas = [
        {"name": "正方辩手X", "stance": "为 A 全力辩护"},
        {"name": "反方辩手Y", "stance": "为 B 全力辩护"},
        {"name": "裁判Z", "stance": "只看证据"},
    ]
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament",
        json={"rounds": 2, "personas": personas},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        messages = (
            (
                await session.execute(
                    select(ReviewMessage)
                    .join(ReviewSession, ReviewSession.id == ReviewMessage.session_id)
                    .where(ReviewSession.target_type == "idea_match")
                    .order_by(ReviewMessage.round)
                )
            )
            .scalars()
            .all()
        )
        # rounds=2：正/反方各 2 轮 + 裁判 = 5 条
        assert [m.author_name for m in messages] == [
            "正方辩手X",
            "反方辩手Y",
            "正方辩手X",
            "反方辩手Y",
            "裁判Z",
        ]
        assert [m.round for m in messages] == [1, 2, 3, 4, 5]
    assert idea_a  # 引用消除 lint 告警


async def test_pick_sets_status_directly_without_a_gate(client, queue_stub, bus_recorder):
    """选中 = 直接置 promoted：不建闸门、不用自己审批自己；再点一次是空操作。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id, "要选中的想法", status="under_review")

    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == idea_id and resp.json()["status"] == "promoted"
    assert (await client.get(f"/api/ideas/{idea_id}", headers=headers)).json()[
        "status"
    ] == "promoted"

    # 没有任何闸门，也没有 gate.created 通知；与任务无关所以不入队
    for status_filter in ("pending", "decided"):
        resp = await client.get(
            f"/api/gates?project_id={project_id}&status={status_filter}", headers=headers
        )
        assert resp.json() == []
    assert not [m for _, m in bus_recorder.notify if m["type"] == "gate.created"]
    assert queue_stub.jobs == []
    status_events = [m for _, m in bus_recorder.notify if m["type"] == "idea.status"]
    assert status_events == [{"type": "idea.status", "idea_id": idea_id, "status": "promoted"}]

    # 已选中再选一次：200 原样返回，活动流里只记一次
    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "promoted"
    async with get_sessionmaker()() as session:
        picked = (
            (await session.execute(select(Activity).where(Activity.kind == "idea.promoted")))
            .scalars()
            .all()
        )
    assert len(picked) == 1 and picked[0].payload == {"idea_id": idea_id}


async def test_patch_idea_pick_unpick_reject(client, queue_stub, bus_recorder):
    """PATCH 可以选中、取消选中、淘汰，都能反悔；under_review 只由评审任务写。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id, "来回改状态的想法")

    resp = await client.patch(
        f"/api/ideas/{idea_id}", json={"status": "under_review"}, headers=headers
    )
    assert resp.status_code == 422
    for target in ("promoted", "candidate", "rejected", "candidate"):
        resp = await client.patch(f"/api/ideas/{idea_id}", json={"status": target}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == target
    status_events = [m["status"] for _, m in bus_recorder.notify if m["type"] == "idea.status"]
    assert status_events == ["promoted", "candidate", "rejected", "candidate"]
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    assert resp.json() == []


async def test_idea_counts_exclude_the_recycle_bin(client, queue_stub):
    """界面上的想法计数不算回收站里的。

    扔进回收站却看不到数字变化，人只会以为删除没生效然后再删一次。#397 修好了
    锦标赛那条路，计数这条当时没跟上。
    """
    project_id, headers = await _setup_project(client, name="counts-exclude-trashed")
    for i in range(3):
        await _seed_idea(project_id, f"留着的想法{i}")
    trashed_id = await _seed_idea(project_id, "要扔的想法")

    resp = await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["idea_counts"]["total"] == 4

    assert (await client.delete(f"/api/ideas/{trashed_id}", headers=headers)).status_code == 204

    resp = await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)
    counts = resp.json()["idea_counts"]
    assert counts["total"] == 3, "回收站里的想法仍被计入"
    assert counts["candidate"] == 3
