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
from app.services.concepts import library_concept_ids
from app.services.libraries import get_source_library_ids, visible_library_ids_stmt
from app.services.projects import in_my_projects

_SNIPPET_CHARS = 120


def _snippet(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else "")


async def global_search(
    session: AsyncSession,
    *,
    q: str,
    limit_per_type: int = 5,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[GlobalSearchHit]:
    """跨实体检索。**作用域二选一**：

    - ``user_id``：用户够得着的全部资产（顶栏 ⌘K 走这条）。判据复用
      :func:`~app.services.libraries.visible_library_clause` 与
      :func:`~app.services.projects.in_my_projects`，与列表页、详情页同一口径——
      搜索比它们宽就是越权，比它们窄就是「明明收录了却搜不到」。
    - ``project_id``：只搜这个课题（agent 工具 ``global_search`` 走这条，它检索的
      本来就是课题内的想法/实验/稿件）。

    加新类型时注意：凡是带 ``trashed_at`` 的实体都要排掉回收站里的。搜索是回收站最容易
    漏掉的出口——列表页都记得过滤，搜索却把删掉的东西照样捞回来，点进去还是活的。
    目前 idea / experiment / manuscript 三种是软删的。
    """
    if (project_id is None) == (user_id is None):
        raise ValueError("global_search 需要且只需要 project_id 或 user_id 其一")

    pattern = f"%{q}%"
    hits: list[GlobalSearchHit] = []

    if project_id is not None:
        # 课题作用域：论文/概念按课题关联库并集检索；无关联库 = 无语料，跳过这两类。
        library_scope: object = await get_source_library_ids(session, project_id)
        no_corpus = not library_scope

        def scoped(col):
            return col == project_id
    else:
        assert user_id is not None
        library_scope = visible_library_ids_stmt(user_id)
        no_corpus = False  # 子查询为空时自然搜不到，不必先查一次

        def scoped(col):
            return in_my_projects(col, user_id)

    # 跨库并集：group_by(Paper.id) 去掉同一论文命中多库的重复行（状态取任一非回收站行）
    paper_rows = (
        (
            await session.execute(
                select(Paper, func.min(LibraryPaper.status))
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(
                    LibraryPaper.library_id.in_(library_scope),
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
        if not no_corpus
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
                        Concept.id.in_(library_concept_ids(library_scope)),
                        or_(Concept.name.ilike(pattern), Concept.definition.ilike(pattern)),
                    )
                    .order_by(Concept.updated_at.desc())
                    .limit(limit_per_type)
                )
            )
            .scalars()
            .all()
        )
        if not no_corpus
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
                    scoped(Idea.project_id),
                    Idea.trashed_at.is_(None),
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
            .where(
                scoped(Experiment.project_id),
                Experiment.trashed_at.is_(None),
                Idea.trashed_at.is_(None),
                Idea.title.ilike(pattern),
            )
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
                .where(scoped(VoyageRun.project_id), VoyageRun.goal.ilike(pattern))
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
                .where(
                    scoped(Manuscript.project_id),
                    Manuscript.trashed_at.is_(None),
                    Manuscript.title.ilike(pattern),
                )
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
