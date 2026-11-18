"""本机能力：界面据此决定哪些入口可用（研究页、写作页、实验页共用）。

以前只在研究详情（GET /research/{id}）里顺带给出，写作页、实验页想知道
「能不能编译」「有没有机器」就得先有一次研究；而且 latex 只认 tectonic，
装了 TeX Live 的机器被说成不能编译。这里是唯一的计算处。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ssh_credential import ConnectionCredential
from app.schemas.research import Capabilities
from app.services import latex_compile


async def local_capabilities(session: AsyncSession, user_id: uuid.UUID) -> Capabilities:
    has_machine = (
        await session.execute(
            select(ConnectionCredential.id).where(ConnectionCredential.user_id == user_id).limit(1)
        )
    ).first() is not None
    return Capabilities(remote_machine=has_machine, latex=latex_compile.latex_available())
