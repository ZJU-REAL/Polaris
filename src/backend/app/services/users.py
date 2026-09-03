"""用户相关业务逻辑（不 import fastapi）：查人（加协作者）与 token 用量统计。

管理员用户管理（列表/编辑/删除/批量分配）已随去实验室化移除（#603）。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_config import LLMUsage
from app.models.user import User


async def search_users(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 10,
    exclude_ids: list[uuid.UUID] | None = None,
) -> list[User]:
    """按 email / 显示名模糊查平台用户（加协作者用）；空查询返回空。"""
    q = query.strip()
    if not q:
        return []
    like = f"%{q}%"
    stmt = select(User).where(User.email.ilike(like) | User.display_name.ilike(like))
    if exclude_ids:
        stmt = stmt.where(User.id.not_in(exclude_ids))
    stmt = stmt.order_by(User.display_name, User.email).limit(min(limit, 25))
    return list((await session.execute(stmt)).scalars())


async def tokens_used_by_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(
        func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0)
    ).where(LLMUsage.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one())
