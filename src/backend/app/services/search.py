"""跨实体全局搜索（顶栏 ⌘K）：论文 / 概念 / 想法 / 实验 / AI 任务 / 稿件。

纯确定性 ilike 匹配，每类限量、按更新时间倒序 —— 不走 LLM。
实验本身没有标题，用关联想法的标题匹配与展示。
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.manuscript import Manuscript
from app.models.paper import Concept, Paper
from app.models.voyage import VoyageRun
from app.schemas.search import GlobalSearchHit
from app.services.libraries import get_source_library_ids

_SNIPPET_CHARS = 120


def _snippet(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else "")


async def global_search(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    q: str,
    limit_per_type: int = 5,
) -> list[GlobalSearchHit]:
    pattern = f"%{q}%"
    hits: list[GlobalSearchHit] = []
    # 论文/概念按课题关联库并集检索；无关联库 = 无语料时跳过它们，
    # ideas/实验/航程/稿件等课题作用域实体照常匹配（不受关联库影响）。
    library_ids = await get_source_library_ids(session, project_id)

    # 跨库并集：group_by(Paper.id) 去掉同一论文命中多库的重复行（状态取任一非回收站行）
    paper_rows = (
        (
            await session.execute(
                select(Paper, func.min(LibraryPaper.status))
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(
                    LibraryPaper.library_id.in_(library_ids),
                    LibraryPaper.status != "excluded",  # 回收站不出现在搜索里
                    or_(
                        Paper.title.ilike(pattern),
                        Paper.abstract.ilike(pattern),
                        Paper.tldr.ilike(pattern),
                    ),
                )
                .group_by(Paper.id)
                .order_by(func.max(Paper.updated_at).desc())
                .limit(limit_per_type)
            )
        ).all()
        if library_ids
        else []
    )
    hits += [
        GlobalSearchHit(
            type="paper",
            id=p.id,
            title=p.title,
            snippet=_snippet(p.tldr or p.abstract),
            status=status,
        )
        for p, status in paper_rows
    ]

    concepts = (
        (
            (
                await session.execute(
                    select(Concept)
                    .where(
                        Concept.library_id.in_(library_ids),
                        or_(Concept.name.ilike(pattern), Concept.definition.ilike(pattern)),
                    )
                    .order_by(Concept.updated_at.desc())
                    .limit(limit_per_type)
                )
            )
            .scalars()
            .all()
        )
        if library_ids
        else []
    )
    hits += [
        GlobalSearchHit(type="concept", id=c.id, title=c.name, snippet=_snippet(c.definition))
        for c in concepts
    ]

    ideas = (
        (
            await session.execute(
                select(Idea)
                .where(
                    Idea.project_id == project_id,
                    or_(Idea.title.ilike(pattern), Idea.summary.ilike(pattern)),
                )
                .order_by(Idea.updated_at.desc())
                .limit(limit_per_type)
            )
        )
        .scalars()
        .all()
    )
    hits += [
        GlobalSearchHit(
            type="idea", id=i.id, title=i.title, snippet=_snippet(i.summary), status=i.status
        )
        for i in ideas
    ]

    experiments = (
        await session.execute(
            select(Experiment, Idea.title)
            .join(Idea, Experiment.idea_id == Idea.id)
            .where(Experiment.project_id == project_id, Idea.title.ilike(pattern))
            .order_by(Experiment.updated_at.desc())
            .limit(limit_per_type)
        )
    ).all()
    hits += [
        GlobalSearchHit(type="experiment", id=exp.id, title=idea_title, status=exp.status)
        for exp, idea_title in experiments
    ]

    voyages = (
        (
            await session.execute(
                select(VoyageRun)
                .where(VoyageRun.project_id == project_id, VoyageRun.goal.ilike(pattern))
                .order_by(VoyageRun.updated_at.desc())
                .limit(limit_per_type)
            )
        )
        .scalars()
        .all()
    )
    hits += [
        GlobalSearchHit(
            type="voyage",
            id=v.id,
            title=_snippet(v.goal) or v.kind,
            snippet=v.kind,
            status=v.status,
        )
        for v in voyages
    ]

    manuscripts = (
        (
            await session.execute(
                select(Manuscript)
                .where(Manuscript.project_id == project_id, Manuscript.title.ilike(pattern))
                .order_by(Manuscript.updated_at.desc())
                .limit(limit_per_type)
            )
        )
        .scalars()
        .all()
    )
    hits += [
        GlobalSearchHit(type="manuscript", id=m.id, title=m.title, status=m.status)
        for m in manuscripts
    ]

    return hits
