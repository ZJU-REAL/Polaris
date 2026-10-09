"""PDF 下载失败后，补全照常往下走（向量、打分）——而不是在后面的阶段崩掉。

下载失败会回滚会话，回滚让会话里的所有 ORM 对象过期——不只是 paper，还有调用方
传进来的目标库 ``target``。之后再同步读 ``target.discipline`` 就是一次异步会话里的
惰性加载，抛 ``MissingGreenlet``，整个补全任务失败：没有论文级向量、没有相关性分。
离线 / 代理不通时每一篇手动添加的 arXiv 论文都会这样。
"""

import uuid

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper
from app.services import paper_enrich
from app.services.libraries import get_membership
from app.services.literature import sources as literature_sources
from tests.conftest import add_paper, make_project_with_library, register_and_login


class _DeadArxiv:
    async def download_pdf(self, arxiv_id: str) -> bytes:  # noqa: ARG002
        raise ConnectionError("All connection attempts failed")


async def test_enrich_continues_after_pdf_download_fails(client, fake_redis, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="dead-download", statement="Planning with language models"
    )

    real_require = literature_sources.require_source

    def _require(source_id: str, **kwargs):
        if source_id == "arxiv":
            return _DeadArxiv()
        return real_require(source_id, **kwargs)

    monkeypatch.setattr(literature_sources, "require_source", _require)

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Tree of Thoughts",
            abstract="Deliberate problem solving with large language models.",
            arxiv_id="2305.10601",
            status="included",
        )
        await session.commit()
        paper_id = paper.id

    events: list[tuple[str, str]] = []

    async def emit(stage: str, status: str, detail: str | None = None) -> None:  # noqa: ARG001
        events.append((stage, status))

    async with get_sessionmaker()() as session:
        target = await session.get(DirectionLibrary, uuid.UUID(str(library_id)))
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=target, user_id=None, project_id=None, emit=emit
        )

    assert ("download", "error") in events
    assert ("extract", "skipped") in events
    # 下载失败之后的阶段都还在跑，并且有结果
    assert ("embed", "ok") in events, events
    assert ("score", "ok") in events, events

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert paper.pdf_path is None
        assert await paper_enrich.has_current_paper_vector(session, paper)
        membership = await get_membership(
            session, library_id=uuid.UUID(str(library_id)), paper_id=paper_id
        )
        assert membership is not None and membership.relevance_score is not None
