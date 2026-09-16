"""新用户的开场清单（#801 第 3 步）。

公有云上一个人注册完之后面对的是一个什么都还没配的平台：没有模型就什么 AI 功能
都用不了，没有文献库就没有语料，没有机器就跑不了通用实验。这里把这几件事算出来。

四项都从真实状态算，不存「你走到第几步」：存进度必然会和实际状态对不上——删掉
唯一的文献库，进度条还停在「已完成」——而真实状态顺带回答了「为什么这个功能报错」。

只出 id 和布尔，不出文案：界面文案走前端 tr(zh,en)，后端塞中文串会让中英切换
对这张卡片失效。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary
from app.models.ssh_credential import SSHCredential
from app.models.user import User

#: 存在 ``User.settings`` 里的命名空间键（#737 的分层约定）。
DISMISSED_KEY = "onboarding.dismissed"


async def _has_model(user: User) -> bool:
    """这个用户能不能真的打到一个对话模型。

    问路由表而不是「他名下有没有 provider」：部署级配好了、他自己没配，同样是
    能用的——自部署就是这种形状，不该反过来催他再配一份（#803 的合并语义）。
    """
    from app.core.llm.router import get_llm_router

    return "default" in await get_llm_router().configured_stages(user.id)


async def _library_counts(session: AsyncSession, user: User) -> tuple[int, int]:
    """(自己建的库数量, 其中声明了学科的数量)。"""

    async def count(*where: Any) -> int:
        stmt = select(func.count()).select_from(DirectionLibrary).where(*where)
        return int((await session.execute(stmt)).scalar_one())

    mine = DirectionLibrary.submitted_by == user.id
    return await count(mine), await count(mine, DirectionLibrary.discipline.is_not(None))


async def _has_machine(session: AsyncSession, user: User) -> bool:
    stmt = (
        select(func.count())
        .select_from(SSHCredential)
        .where(SSHCredential.user_id == user.id)
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def checklist(session: AsyncSession, user: User) -> dict[str, Any]:
    """这个用户的开场清单。

    ``dismissed`` 为真时前端不展示，但内容照算——收起来的是提示，不是事实。
    """
    libraries, with_discipline = await _library_counts(session, user)
    items = [
        {"id": "model", "done": await _has_model(user)},
        {"id": "library", "done": libraries > 0},
        # 「通用」是一个正当答案，所以这一项可能永远不被完成；文案里要说清
        # 这点，别让它看起来像一个没做完的任务
        {"id": "discipline", "done": with_discipline > 0},
        {"id": "experiment", "done": await _has_machine(session, user)},
    ]
    return {
        "items": items,
        "dismissed": bool(user.setting(DISMISSED_KEY, False)),
        "done": all(item["done"] for item in items),
    }


async def dismiss(session: AsyncSession, user: User) -> None:
    """记下「别再提示我了」。

    整体重新赋值而不是原地改：``settings`` 是 JSON 列，就地改 dict 不会被
    SQLAlchemy 识别为脏数据，提交下去等于什么都没存。
    """
    user.settings = {**(user.settings or {}), DISMISSED_KEY: True}
    await session.commit()
