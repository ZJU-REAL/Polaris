"""浏览打点：前端在打开文献库/论文时调一次。

**只写不读**：读走 /lab/hot。分开是因为写这条路要极轻——它挂在每一次打开动作上，
不能因为统计而拖慢正事，也不该在失败时影响页面。
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.models.view_event import VIEW_KINDS
from app.services import view_stats

router = APIRouter(prefix="/views", tags=["views"])


class ViewCreate(BaseModel):
    kind: str = Field(pattern="^(library|paper)$")
    target_id: uuid.UUID


@router.post("", status_code=202)
async def record_view(
    payload: ViewCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, bool]:
    """记一次浏览。返回 counted=false 表示被去重窗口挡掉了（不是错误）。

    202 而不是 201：这是一条**尽力而为**的记录，调用方不该等它、也不该因为它失败而
    改变行为。
    """
    assert payload.kind in VIEW_KINDS
    counted = await view_stats.record_view(
        session, kind=payload.kind, target_id=payload.target_id, user_id=user.id
    )
    return {"counted": counted}
