"""个人版 wiki 按需编译（P5b，不 import fastapi）。

适用对象：**没有任何库版 wiki** 的内容池论文（典型：个人补充入库的库外论文，
或入库了但还没编译的论文）。用通用模板（无 rubric）编译，可选带上课题
statement 作为侧重提示；结果写进调用者本人的 user_library_entries.wiki_content
（三层解析「库版实时 > 个人版 > 书架快照」的中间层）。

费用归个人（LLM 用量记 user_id；给了 topic_id 时顺带归因该课题）。
并发防抖：同一 paper × user 编译进行中时，二次请求直接抛
CompileInProgressError（路由映射 409）——进程内 set 实现，单实例足够，
多副本部署下最坏情况是重复编译一次、后写覆盖，无一致性风险。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import get_llm_router
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.project import Project
from app.services.user_library import set_personal_wiki
from app.services.wiki_compile import CompiledWiki, compile_paper


class LibraryWikiExistsError(Exception):
    """已有库版 wiki，不需要个人编译（前端应直接展示库版）。"""


class CompileInProgressError(Exception):
    """同一 paper × user 的编译已在进行中。"""


# 进行中的编译（paper_id, user_id）；见模块 docstring 的并发说明
_COMPILING: set[tuple[uuid.UUID, uuid.UUID]] = set()


async def has_library_wiki(session: AsyncSession, paper_id: uuid.UUID) -> bool:
    """任一方向库的成员行上有 wiki_content 即视为「有库版」。"""
    stmt = (
        select(LibraryPaper.id)
        .where(LibraryPaper.paper_id == paper_id, LibraryPaper.wiki_content.is_not(None))
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def compile_personal_wiki(
    session: AsyncSession,
    *,
    paper: Paper,
    user_id: uuid.UUID,
    topic_id: uuid.UUID | None = None,
) -> CompiledWiki:
    """通用模板编译个人版 wiki 并写进本人个人库条目，返回编译结果。

    topic_id（可选，调用方已验成员身份）：课题 statement 作为侧重提示，
    同时作为 LLM 用量的课题归因。
    """
    if await has_library_wiki(session, paper.id):
        raise LibraryWikiExistsError(str(paper.id))

    statement: str | None = None
    if topic_id is not None:
        project = await session.get(Project, topic_id)
        if project is not None:
            statement = project.statement or project.name

    key = (paper.id, user_id)
    if key in _COMPILING:
        raise CompileInProgressError(str(paper.id))
    _COMPILING.add(key)
    try:
        compiled = await compile_paper(
            paper,
            statement=statement,
            llm=get_llm_router(),
            user_id=user_id,
            project_id=topic_id,
        )
    finally:
        _COMPILING.discard(key)
    await set_personal_wiki(session, user_id=user_id, paper=paper, wiki_content=compiled.content)
    return compiled
