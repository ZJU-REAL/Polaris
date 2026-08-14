"""工具的检索范围：一个课题，还是这个人能看见的全部文献库。

平台里的论文本来就是**按库**组织的，「课题」只是库的一个集合（``TopicSourceLibrary``）。
所以放开范围不需要改检索本身，只需要换一种方式回答「这次能搜哪些库」：

- 课题内（voyage、课题里的对话）：课题关联的那些库；
- 全局（PolarisBuddy）：这个人**看得见**的全部库（管理员看全部，普通用户看自己的
  个人库加全部公共库，口径与 ``library_visible_to`` 一致）。

两条路都落到同一批库 id 上，越权与否由那一步决定，检索代码不需要知道自己在哪种
模式下跑。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.project import ProjectMember
from app.models.user import User
from app.services.libraries import (
    get_source_library_ids,
    library_visible_to,
    membership_rank,
)
from app.services.papers import PaperView, paper_in_daily_feed
from app.tools.context import ToolContext


async def visible_library_ids(session: AsyncSession, user: User) -> list[uuid.UUID]:
    """这个人看得见的全部库。口径与库列表页一致——助手不该比界面看得更多。"""
    libraries = (
        (await session.execute(select(DirectionLibrary).order_by(DirectionLibrary.created_at)))
        .scalars()
        .all()
    )
    return [lib.id for lib in libraries if library_visible_to(lib, user)]


async def library_ids_for(session: AsyncSession, ctx: ToolContext) -> list[uuid.UUID]:
    """这次检索能搜哪些库。

    ``ctx.library_ids`` 显式给了就用它（全局助手在建 context 时已按可见性算好），
    否则退回课题关联库。**空列表是合法结果**：没有语料就查不到东西，调用方返回
    空态而不是报错——这与"课题没关联任何库"是同一件事。
    """
    if ctx.library_ids is not None:
        return list(ctx.library_ids)
    return await get_source_library_ids(session, ctx.project_id)


async def project_ids_for(session: AsyncSession, ctx: ToolContext) -> list[uuid.UUID]:
    """这次能看哪些课题的资产（实验、想法、稿件）。

    :func:`library_ids_for` 的对偶。选了课题就只有它；**没选就是这个人参与的全部
    课题**——「所有资产」是放开范围，不是没有范围。

    这里曾经直接拿 ``ctx.project_id`` 去比：不选课题时它是 None，SQL 里就成了
    ``project_id = NULL``，永远匹配不上。于是助手在默认状态下报「项目内不存在该
    实验」，而那个实验就在用户眼前的页面上。空列表是合法结果（这人一个课题都没
    参与），调用方给空态，不报错。
    """
    if ctx.project_id is not None:
        return [ctx.project_id]
    if ctx.user_id is None:
        return []
    rows = await session.execute(
        select(ProjectMember.project_id).where(ProjectMember.user_id == ctx.user_id)
    )
    return list(rows.scalars().all())


async def membership_in_scope(
    session: AsyncSession, ctx: ToolContext, paper_id: uuid.UUID
) -> LibraryPaper | None:
    """这篇论文在本次范围内的成员行；不在范围内返回 None。

    这是「能不能看这篇」的唯一检查点：范围由 :func:`library_ids_for` 算出，越权
    在那一步就已经挡住了。跨库同一篇取确定性视角（相关性高的库优先），与
    ``membership_for_project`` 同规则。
    """
    library_ids = await library_ids_for(session, ctx)
    if not library_ids:
        return None
    rows = (
        (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id.in_(library_ids),
                    LibraryPaper.paper_id == paper_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return min(rows, key=membership_rank)


@dataclass(frozen=True, slots=True)
class PaperAccess:
    """一篇读得到的论文，外加「它进库了没有」。

    ``in_library`` 为假就是每日新论文池里那种**还没被任何库收录**的论文。工具要把
    这个区别如实告诉模型，否则它会把一篇今天刚出的新论文说成「你库里的工作」。
    """

    view: PaperView
    in_library: bool


async def paper_access(
    session: AsyncSession, ctx: ToolContext, paper_id: uuid.UUID, *, with_concepts: bool = False
) -> PaperAccess | None:
    """这次能不能读这篇论文；读不到返回 None。**所有**按 id 读单篇论文的工具走这里。

    先按本次范围取成员行（:func:`membership_in_scope`），范围外再看它在不在每日新论文
    池里——每日新论文全实验室可读（``paper_in_daily_feed``，与 HTTP 端点的池级兜底同
    一条判据），登录用户就能读。

    为什么非兜底不可：``search_daily_pool`` 交给模型的，按定义就是**尚未收录进任何库**
    的当日新论文。只认成员行的话，助手拿自己上一步搜出来的 id 挨个读详情，20 篇能错
    20 篇，还都报「库内不存在该论文」——而那些论文就在用户眼前的每日新论文页上。这与
    :func:`project_ids_for` 修过的是同一类毛病：范围判严了，症状却长得像数据没了。

    兜底只开每日池这一条：它是唯一会把库外论文 id 交到模型手上的工具。书架 / 个人库
    那些 HTTP 端点也放行的路径这里一概不认——工具没有渠道拿到那些 id，放开只会平白
    松掉课题范围。
    """
    membership = await membership_in_scope(session, ctx, paper_id)
    options = [selectinload(Paper.concepts)] if with_concepts else None
    if membership is not None:
        paper = await session.get(Paper, paper_id, options=options)
        if paper is not None:
            return PaperAccess(PaperView(paper, membership, ctx.project_id), True)
    if ctx.user_id is None:  # 系统内部调用没有身份，每日池的「登录即可读」无从谈起
        return None
    if not await paper_in_daily_feed(session, paper_id):
        return None
    paper = await session.get(Paper, paper_id, options=options)
    if paper is None:
        return None
    # 临时成员行（不入 session、永不落库），口径同 services/papers 的池级兜底视角。
    # status=candidate：它确实还只是个候选，别让它看起来像已收录。
    pooled = LibraryPaper(
        status="candidate", created_at=paper.created_at, updated_at=paper.updated_at
    )
    return PaperAccess(PaperView(paper, pooled, None), False)


async def readable_paper(
    session: AsyncSession, ctx: ToolContext, raw_id: Any, *, with_concepts: bool = False
) -> PaperAccess:
    """:func:`paper_access` 加上参数解析与报错，给「拿一个 id 读一篇」的工具用。"""
    try:
        paper_id = uuid.UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError(f"paper_id 不是合法 uuid：{raw_id}") from e
    access = await paper_access(session, ctx, paper_id, with_concepts=with_concepts)
    if access is None:
        # 不可见与不存在一律同一口径，不泄露存在性；但话要说清找过哪几处，
        # 否则「库内不存在」会让人以为论文丢了。
        raise ValueError(f"读不到这篇论文（不在本次能检索的文献库，也不在每日新论文里）：{raw_id}")
    return access
