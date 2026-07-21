"""用户管理业务逻辑（不 import fastapi）：管理员用户列表 / 编辑 / 批量分配方向。"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_config import LLMUsage
from app.models.project import Project, ProjectMember
from app.models.user import FEATURE_KEYS, User


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


async def list_users_with_usage(session: AsyncSession) -> list[dict[str, Any]]:
    usage_sq = (
        select(
            LLMUsage.user_id.label("uid"),
            func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens).label("tokens"),
        )
        .group_by(LLMUsage.user_id)
        .subquery()
    )
    stmt = (
        select(User, func.coalesce(usage_sq.c.tokens, 0))
        .outerjoin(usage_sq, usage_sq.c.uid == User.id)
        .order_by(User.created_at)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "has_avatar": u.has_avatar,
            "token_quota": u.token_quota,
            "features": u.features,
            "llm_access": u.llm_access,
            "llm_self_managed": u.llm_self_managed,
            "tokens_used": int(tokens),
            "created_at": u.created_at,
        }
        for u, tokens in rows
    ]


async def admin_update_user(session: AsyncSession, user: User, data: dict[str, Any]) -> User:
    """应用管理员编辑；features 只保留已知功能键；token_quota=-1 清除配额。"""
    if (v := data.get("display_name")) is not None:
        user.display_name = v
    if (v := data.get("role")) is not None:
        user.role = v
    if (v := data.get("is_active")) is not None:
        user.is_active = v
    if (v := data.get("token_quota")) is not None:
        user.token_quota = None if v == -1 else v
    if (v := data.get("llm_access")) is not None:
        user.llm_access = v
    if (v := data.get("llm_self_managed")) is not None:
        user.llm_self_managed = v
    if (v := data.get("features")) is not None:
        user.features = {k: bool(v[k]) for k in v if k in FEATURE_KEYS} or None
    await session.commit()
    await session.refresh(user)
    # 接管状态改变需让路由器缓存失效（否则最长 60s 内旧配置仍生效）
    if "llm_self_managed" in data:
        from app.core.llm.router import get_llm_router

        get_llm_router().invalidate_cache()
    return user


async def batch_assign(
    session: AsyncSession,
    *,
    user_ids: list[uuid.UUID],
    project_ids: list[uuid.UUID],
    role: str,
) -> int:
    """把一批用户加入一批方向（已是成员的跳过），返回新增成员数。"""
    valid_users = set(
        (await session.execute(select(User.id).where(User.id.in_(user_ids)))).scalars()
    )
    valid_projects = set(
        (await session.execute(select(Project.id).where(Project.id.in_(project_ids)))).scalars()
    )
    existing = set(
        (
            await session.execute(
                select(ProjectMember.project_id, ProjectMember.user_id).where(
                    ProjectMember.project_id.in_(valid_projects),
                    ProjectMember.user_id.in_(valid_users),
                )
            )
        ).all()
    )
    added = 0
    for pid in valid_projects:
        for uid in valid_users:
            if (pid, uid) in existing:
                continue
            session.add(ProjectMember(project_id=pid, user_id=uid, role=role))
            added += 1
    await session.commit()
    return added
