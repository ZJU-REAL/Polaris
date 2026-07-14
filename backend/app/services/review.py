"""评审会话 / 消息 / Elo 业务逻辑（不 import fastapi）。

会话类型约定见 models/review.py：idea_match（target_id=idea_a）/ idea_discussion / manuscript。
"""

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idea import Idea
from app.models.review import ReviewMessage, ReviewSession
from app.models.user import User

# 默认三人设（docs/api-m3.md §3）：顺序约定 [0]=正方 [1]=反方 [2]=裁判
DEFAULT_PERSONAS: list[dict[str, str]] = [
    {"name": "严谨方法论者", "stance": "专挑方法与实验设计的漏洞，重视消融实验与统计显著性"},
    {"name": "务实工程师", "stance": "关注可实现性、工程成本与复现难度，反对不可落地的空中楼阁"},
    {"name": "领域怀疑论者", "stance": "质疑新颖性与真实影响力，逼问与现有工作的本质区别"},
]

ELO_K = 32.0
ELO_INITIAL = 1200.0


def elo_update(
    rating_a: float, rating_b: float, winner: str, k: float = ELO_K
) -> tuple[float, float]:
    """标准 Elo：winner ∈ {"a", "b"}，返回 (new_a, new_b)。"""
    expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))
    score_a = 1.0 if winner == "a" else 0.0
    new_a = rating_a + k * (score_a - expected_a)
    new_b = rating_b + k * ((1.0 - score_a) - (1.0 - expected_a))
    return new_a, new_b


def serialize_message(message: ReviewMessage) -> dict[str, Any]:
    """ReviewMessageRead 形状的 dict（WS review.message 事件载荷用）。"""
    return {
        "id": str(message.id),
        "session_id": str(message.session_id),
        "author_type": message.author_type,
        "author_name": message.author_name,
        "content": message.content,
        "round": message.round,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


# ---- 会话 ----


async def get_or_create_discussion(session: AsyncSession, idea_id: uuid.UUID) -> ReviewSession:
    """idea 常驻讨论区：首次访问惰性创建（幂等）。"""
    stmt = select(ReviewSession).where(
        ReviewSession.target_type == "idea_discussion", ReviewSession.target_id == idea_id
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing
    discussion = ReviewSession(
        target_type="idea_discussion", target_id=idea_id, payload={"idea_id": str(idea_id)}
    )
    session.add(discussion)
    await session.commit()
    await session.refresh(discussion)
    return discussion


async def list_idea_sessions(session: AsyncSession, idea_id: uuid.UUID) -> list[ReviewSession]:
    """该 idea 的讨论区 + 参与的全部辩论场次（idea_a 走 target_id，idea_b 走 payload）。"""
    stmt = (
        select(ReviewSession)
        .where(
            or_(
                and_(
                    ReviewSession.target_type == "idea_discussion",
                    ReviewSession.target_id == idea_id,
                ),
                and_(
                    ReviewSession.target_type == "idea_match",
                    or_(
                        ReviewSession.target_id == idea_id,
                        ReviewSession.payload["idea_b"].as_string() == str(idea_id),
                    ),
                ),
            )
        )
        .order_by(ReviewSession.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def session_project_id(
    session: AsyncSession, review_session: ReviewSession
) -> uuid.UUID | None:
    """会话归属项目（权限判定用）：idea 系会话经 target_id 指向的 idea 解析。"""
    if review_session.target_type in ("idea_discussion", "idea_match"):
        idea = await session.get(Idea, review_session.target_id)
        return idea.project_id if idea is not None else None
    return None


# ---- 消息 ----


async def list_messages(session: AsyncSession, session_id: uuid.UUID) -> list[ReviewMessage]:
    stmt = (
        select(ReviewMessage)
        .where(ReviewMessage.session_id == session_id)
        .order_by(ReviewMessage.round, ReviewMessage.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def next_round(session: AsyncSession, session_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(ReviewMessage.round), 0)).where(
        ReviewMessage.session_id == session_id
    )
    return int((await session.execute(stmt)).scalar_one()) + 1


async def add_human_message(
    session: AsyncSession, review_session: ReviewSession, user: User, content: str
) -> ReviewMessage:
    message = ReviewMessage(
        session_id=review_session.id,
        author_type="human",
        author_id=user.id,
        author_name=user.display_name,
        content=content,
        round=await next_round(session, review_session.id),
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def human_comments(session: AsyncSession, idea_id: uuid.UUID) -> list[str]:
    """该 idea 讨论区内全部 human 消息（「作者：内容」行），注入辩论/打分 agent 上下文。"""
    stmt = (
        select(ReviewMessage)
        .join(ReviewSession, ReviewSession.id == ReviewMessage.session_id)
        .where(
            ReviewSession.target_type == "idea_discussion",
            ReviewSession.target_id == idea_id,
            ReviewMessage.author_type == "human",
        )
        .order_by(ReviewMessage.created_at)
    )
    messages = (await session.execute(stmt)).scalars().all()
    return [f"{m.author_name or '匿名'}：{m.content}" for m in messages]
