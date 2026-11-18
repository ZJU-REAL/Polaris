"""本机能力（GET /capabilities）：能不能编译 LaTeX、有没有登记过远程机器。

和研究详情里的 ``capabilities`` 是同一个计算（services.capabilities），只是不依赖
任何一次研究——写作页、实验页打开时就要知道，而那时可能一次研究都没有。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.research import Capabilities
from app.services import capabilities as capabilities_service

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("", response_model=Capabilities)
async def get_capabilities(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Capabilities:
    return await capabilities_service.local_capabilities(session, user.id)
