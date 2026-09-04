"""用户相关业务逻辑（不 import fastapi）：token 用量统计。

管理员用户管理（列表/编辑/删除/批量分配）已随去实验室化移除（#603）。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_config import LLMUsage


async def tokens_used_by_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(
        func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0)
    ).where(LLMUsage.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one())
