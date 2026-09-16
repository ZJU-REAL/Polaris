"""开场清单：新用户登录后还差哪几步（#801 第 3 步）。

只读当前登录用户自己的状态，普通登录即可——这张卡片本来就是给「第二个注册的人」
看的，再挂主人守卫就自相矛盾了。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.services import onboarding as onboarding_service

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class ChecklistItem(BaseModel):
    """一项。``id`` 决定界面显示哪段文案、跳到哪里——文案在前端（tr），不在这里。"""

    id: str
    done: bool


class ChecklistRead(BaseModel):
    items: list[ChecklistItem]
    #: 用户点过「不再提示」。仍返回 items：收起来的是提示，不是事实
    dismissed: bool
    done: bool


@router.get("", response_model=ChecklistRead)
async def get_checklist(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ChecklistRead:
    return ChecklistRead(**await onboarding_service.checklist(session, user))


@router.post("/dismiss", response_model=ChecklistRead)
async def dismiss_checklist(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ChecklistRead:
    """不再提示。返回更新后的清单，省掉前端一次回查。"""
    await onboarding_service.dismiss(session, user)
    return ChecklistRead(**await onboarding_service.checklist(session, user))
