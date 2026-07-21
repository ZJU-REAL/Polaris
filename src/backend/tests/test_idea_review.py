"""Idea 评审锦标赛 / 讨论 / 晋级闸门测试（fake LLM 全离线，直接驱动 VoyageEngine）。

覆盖：辩论落库与 WS 事件、Elo 更新与战绩、leaderboard、讨论区惰性创建 +
human 消息注入辩论上下文、promote → gate → approve 联动、权限。
"""

import uuid

from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.idea import Idea
from app.models.review import ReviewMessage, ReviewSession
from tests.conftest import RecordingBus, register_and_login

DEFAULT_PERSONA_NAMES = ["严谨方法论者", "务实工程师", "领域怀疑论者"]


async def _setup_project(client, email="alice@example.com", name="review-proj"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_idea(project_id: str, title: str, *, status="candidate") -> str:
    async with get_sessionmaker()() as session:
        idea = Idea(
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


async def test_tournament_fans_out_per_match_nodes(client, queue_stub):
    """4 个 idea → 2 场对局：pair 展开 2 个 review.match 节点（每场可见、可逐场查预算），
    顺序 pair → match×2 → summarize，全部通过（docs/voyage-loop.md §7）。"""
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
    assert message["author_name"] == "Alice"
    assert message["round"] == 1
    ws_messages = [m for _, m in bus_recorder.notify if m["type"] == "review.message"]
    assert len(ws_messages) == 1
    assert ws_messages[0]["session_id"] == discussion_id
    assert ws_messages[0]["project_id"] == project_id
    assert ws_messages[0]["message"]["content"] == comment

    resp = await client.get(f"/api/sessions/{discussion_id}/messages", headers=headers)
    assert [m["content"] for m in resp.json()] == [comment]

    # 非成员对 session 一律 404
    token_b = await register_and_login(client, email="stranger@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp = await client.get(f"/api/sessions/{discussion_id}/messages", headers=headers_b)
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/sessions/{discussion_id}/messages", json={"content": "x"}, headers=headers_b
    )
    assert resp.status_code == 404

    # 锦标赛：human 评论注入辩论 agent 上下文（fake LLM 收到的 prompt 含该评论）
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={"rounds": 1}, headers=headers
    )
    run_id = resp.json()["id"]
    provider = _RecordingProvider()
    router = LLMRouter()
    router._providers[("fake", None, "")] = provider
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    debate_prompts = [p for p in provider.prompts if "人类评审意见" in p]
    assert debate_prompts, "辩论 prompt 应包含人类评审意见区块"
    assert any(comment in p and "Alice" in p for p in debate_prompts)


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


async def test_promote_gate_approve_links_idea_status(client, queue_stub, bus_recorder):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id, "待晋级想法", status="under_review")

    # 普通成员（role=member）不可 promote
    await register_and_login(client, email="bob@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 204
    resp = await client.post(
        "/api/auth/jwt/login",
        data={"username": "bob@example.com", "password": "str0ng-password"},
    )
    headers_bob = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers_bob)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "PROMOTE_FORBIDDEN"

    # owner promote → 创建 idea_promotion 闸门（pending）+ gate.created 通知
    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers)
    assert resp.status_code == 201, resp.text
    gate = resp.json()
    assert gate["kind"] == "idea_promotion" and gate["status"] == "pending"
    assert gate["payload"]["idea_id"] == idea_id
    created_events = [m for _, m in bus_recorder.notify if m["type"] == "gate.created"]
    assert created_events and created_events[0]["gate"]["id"] == gate["id"]

    # 已有 pending 晋级闸门 → 409
    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "PROMOTION_ALREADY_PENDING"

    # gates approve 联动：idea.status=promoted + WS idea.status 事件；不入队 resume（无 voyage_id）
    resp = await client.post(f"/api/gates/{gate['id']}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert queue_stub.jobs == []  # 晋级闸门与航程无关
    resp = await client.get(f"/api/ideas/{idea_id}", headers=headers)
    assert resp.json()["status"] == "promoted"
    status_events = [m for _, m in bus_recorder.notify if m["type"] == "idea.status"]
    assert {"type": "idea.status", "idea_id": idea_id, "status": "promoted"} in status_events

    # 已晋级 → 再 promote 409
    resp = await client.post(f"/api/ideas/{idea_id}/promote", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "IDEA_ALREADY_PROMOTED"


async def test_patch_idea_rejected_only(client, queue_stub, bus_recorder):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id, "将被淘汰的想法")

    # 只允许 rejected
    resp = await client.patch(f"/api/ideas/{idea_id}", json={"status": "promoted"}, headers=headers)
    assert resp.status_code == 422
    resp = await client.patch(f"/api/ideas/{idea_id}", json={"status": "rejected"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    status_events = [m for _, m in bus_recorder.notify if m["type"] == "idea.status"]
    assert {"type": "idea.status", "idea_id": idea_id, "status": "rejected"} in status_events

    # 非成员 → 404
    token_b = await register_and_login(client, email="nobody@example.com")
    resp = await client.patch(
        f"/api/ideas/{idea_id}",
        json={"status": "rejected"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404
