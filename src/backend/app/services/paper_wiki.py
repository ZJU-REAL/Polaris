"""论文解读的唯一读写入口（不 import fastapi）。

一篇论文只有一份解读（``paper_wikis``，见 models/paper.PaperWiki）：库内编译、
每日推送编译、单篇重新编译都 upsert 同一行，读解读一律查这里——没有就是没有，
不再有「库版 / 个人版 / 快照」的优先级链。
"""

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.base import utcnow
from app.models.paper import Paper, PaperWiki
from app.models.user import User


async def upsert_wiki(
    session: AsyncSession,
    *,
    paper: Paper,
    content: str,
    model: str | None = None,
    compiled_by: uuid.UUID | None = None,
) -> PaperWiki:
    """写这篇论文的解读：已有则整行覆盖（以最新一次编译为准），没有则新建。

    调用方负责 commit。``compiled_by`` 每次覆盖成最新编译的人。
    """
    # 显式查行，不依赖 paper.wiki 是否已加载（刚 refresh 过的对象上读关系会隐式发 SQL）
    wiki = (
        await session.execute(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
    ).scalar_one_or_none()
    if wiki is None:
        wiki = PaperWiki(
            paper_id=paper.id, content=content, model=model, compiled_by=compiled_by
        )
        session.add(wiki)
    else:
        wiki.content = content
        wiki.model = model
        wiki.compiled_by = compiled_by
        wiki.updated_at = utcnow()  # 内容不变时也算一次新编译
    await session.flush()
    # 内存里的 paper 跟上（后续 link_paper_concepts / 出参都直接读 paper.wiki_content）
    set_committed_value(paper, "wiki", wiki)
    return wiki


async def wikis_for(
    session: AsyncSession, paper_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, PaperWiki]:
    """paper_id → 解读行（没有解读的不在结果里）；列表路径批量取用。"""
    ids = list(paper_ids)
    if not ids:
        return {}
    rows = (
        (await session.execute(select(PaperWiki).where(PaperWiki.paper_id.in_(ids))))
        .scalars()
        .all()
    )
    return {row.paper_id: row for row in rows}


async def content_for(session: AsyncSession, paper_id: uuid.UUID) -> str | None:
    """单篇解读正文（没有则 None）；只有 paper_id、拿不到 Paper 对象时用。"""
    stmt = select(PaperWiki.content).where(PaperWiki.paper_id == paper_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def compiler_names(
    session: AsyncSession, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """编译者 id → 显示名（重新编译前的覆盖提示用）。

    compiled_by 是 SET NULL：人被删掉 / 存量迁移数据留空的一律不在结果里，
    调用方按「未知」显示。"""
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {
        uid: (display_name or "").strip() or email.split("@", 1)[0]
        for uid, display_name, email in rows
    }
