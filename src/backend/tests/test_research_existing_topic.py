"""研究：在已有的课题上开始，以及研究的文献检索不生成简报和趋势。

- 带 ``project_id``：不新建课题、不改课题名；课题已有研究回 409；不是自己的课题回 404；
- 课题正好有一个库就沿用（论文留着、配置不被覆盖），没有或不止一个就新建并加进关联；
- 方向不填时用课题的陈述，没有陈述用课题名；
- 连带删除只删这次研究自己建的；
- 研究的文献检索没有「生成简报」「综合趋势」两步，照样跑完并走到等用户选定；
  普通的文献库检索仍是完整的八步；之后单独生成今日简报照常可用。
"""

import uuid

from sqlalchemy import select

from app.agents.voyage.navigator import wiki_plan
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.project import Project
from app.models.research_digest import LibraryResearchDigest
from app.models.research_run import ResearchRun
from app.models.voyage import VoyageRun, VoyageStep
from app.schemas.ingest import IngestKnobs
from app.services import ingest as ingest_service
from app.services import libraries as libraries_service
from app.services import research_runs as research
from app.services.research_runs import utcnow

# literature / live_queue 是 fixture：导入进来 pytest 才认得；用例参数与它们同名是
# fixture 的正常用法
# ruff: noqa: F811
from tests.test_research_runs import (  # noqa: F401
    DIRECTION,
    _advance,
    _login,
    _run_row,
    literature,
    live_queue,
)

TOPIC_NAME = "我原来就有的课题"
TOPIC_STATEMENT = "可验证的 agent 工作流：原来课题上写的一句话"

FULL_PLAN = [
    "wiki.search_candidates",
    "wiki.score_relevance",
    "wiki.fetch_extract",
    "wiki.compile",
    "wiki.link_concepts",
    "wiki.daily_digest",
    "wiki.trend_synthesize",
    "wiki.update_watermark",
]
RESEARCH_PLAN = [a for a in FULL_PLAN if a not in ("wiki.daily_digest", "wiki.trend_synthesize")]
# 调研检索在第一遍筛选之后多一步：沿引用关系扩展并补筛
RESEARCH_PLAN.insert(2, "wiki.expand_citations")


async def _topic(client, headers, *, name=TOPIC_NAME, statement=TOPIC_STATEMENT) -> str:
    body = {"name": name}
    if statement is not None:
        body["statement"] = statement
    resp = await client.post("/api/projects", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _library(session, *, name, papers=(), definition=None, origin=None) -> DirectionLibrary:
    """一个独立的库（``origin`` 给了就回指那个课题），带几篇已经在库里的论文。"""
    library = DirectionLibrary(name=name, project_id=origin, definition=definition)
    session.add(library)
    await session.flush()
    for title in papers:
        paper = Paper(title=title)
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library.id, paper_id=paper.id, status="compiled", relevance_score=0.9
            )
        )
    await session.flush()
    return library


async def _link(session, project_id: str, libraries) -> None:
    await libraries_service.set_source_libraries(
        session, topic_id=uuid.UUID(project_id), library_ids=[lib.id for lib in libraries]
    )
    await session.commit()


async def _link_ids(project_id: str) -> set[str]:
    """课题关联了哪些库（不比顺序：同一刻建的关联先后不定）。"""
    async with get_sessionmaker()() as session:
        ids = await libraries_service.get_source_library_ids(session, uuid.UUID(project_id))
    assert len(ids) == len(set(ids))
    return {str(i) for i in ids}


async def _step_actions(session, voyage_id) -> list[str]:
    rows = await session.execute(
        select(VoyageStep.action).where(VoyageStep.run_id == voyage_id).order_by(VoyageStep.seq)
    )
    return list(rows.scalars().all())


# ---- 1. 在已有课题上开始 ----


async def test_start_on_existing_topic_attaches_instead_of_duplicating(client, queue_stub):
    headers = await _login(client)
    project_id = await _topic(client, headers)

    resp = await client.post(
        "/api/research",
        json={"project_id": project_id, "direction": DIRECTION, "client_key": "k-existing"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["project_id"] == project_id
    assert created["name"] == TOPIC_NAME and created["direction"] == DIRECTION
    assert created["phase"] == "plan" and created["library_id"] is None
    run_id = created["id"]
    assert queue_stub.jobs == [("research_advance", (run_id,), {"_job_id": f"research-{run_id}"})]

    row = await _advance(run_id, queue_stub)  # 确定范围 → 建库 → 建检索任务
    assert row.phase == "survey" and row.status == "running"
    assert row.phases["plan"]["existing_project"] is True
    assert "reused_library" not in row.phases["plan"]  # 课题原来没有库：这个库是新建的
    async with get_sessionmaker()() as session:
        projects = (await session.execute(select(Project))).scalars().all()
        assert [str(p.id) for p in projects] == [project_id]  # 没有多出一个课题
        assert projects[0].name == TOPIC_NAME  # 名字没被换成简报的标题
        assert projects[0].statement == TOPIC_STATEMENT
        library = await session.get(DirectionLibrary, row.library_id)
        assert library is not None and str(library.project_id) == project_id
        survey = await session.get(VoyageRun, uuid.UUID(row.phases["survey"]["voyage_id"]))
        assert survey.kind == "wiki_bootstrap" and survey.library_id == library.id
        assert survey.checkpoint["params"]["skip_digest"] is True
    assert await _link_ids(project_id) == {str(row.library_id)}
    assert ("run_voyage", (str(survey.id),), {}) in queue_stub.jobs

    # 同一个幂等键再来：原来那一条
    again = await client.post(
        "/api/research",
        json={"project_id": project_id, "direction": "别的", "client_key": "k-existing"},
        headers=headers,
    )
    assert again.status_code == 200 and again.json()["id"] == run_id

    # 这个课题已经有一次研究：换了键、换了方向也不再建
    second = await client.post(
        "/api/research",
        json={"project_id": project_id, "direction": "另一个方向", "client_key": "k-other"},
        headers=headers,
    )
    assert second.status_code == 409 and second.json()["detail"] == "RESEARCH_ALREADY_EXISTS"
    # 方向不填也一样
    bare = await client.post("/api/research", json={"project_id": project_id}, headers=headers)
    assert bare.status_code == 409 and bare.json()["detail"] == "RESEARCH_ALREADY_EXISTS"

    # 没带键、60 秒内、方向相同：算同一次提交（双击、重发）
    dup = await client.post(
        "/api/research",
        json={"project_id": project_id, "direction": f"  {DIRECTION.upper()} "},
        headers=headers,
    )
    assert dup.status_code == 200 and dup.json()["id"] == run_id
    # 过了 60 秒就不算了：这个课题已有研究
    async with get_sessionmaker()() as session:
        (await session.get(ResearchRun, uuid.UUID(run_id))).created_at = utcnow() - (
            research.timedelta(seconds=61)
        )
        await session.commit()
    late = await client.post(
        "/api/research", json={"project_id": project_id, "direction": DIRECTION}, headers=headers
    )
    assert late.status_code == 409 and late.json()["detail"] == "RESEARCH_ALREADY_EXISTS"

    # 不存在的课题
    missing = await client.post(
        "/api/research",
        json={"project_id": str(uuid.uuid4()), "direction": DIRECTION},
        headers=headers,
    )
    assert missing.status_code == 404 and missing.json()["detail"] == "PROJECT_NOT_FOUND"
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(ResearchRun))).scalars().all()) == 1
        assert len((await session.execute(select(Project))).scalars().all()) == 1

    # 连带删除：这次研究建的库删掉，原来就有的课题留着
    library_id = row.library_id
    resp = await client.delete(f"/api/research/{run_id}?purge=true", headers=headers)
    assert resp.status_code == 204
    async with get_sessionmaker()() as session:
        assert await session.get(ResearchRun, uuid.UUID(run_id)) is None
        assert await session.get(DirectionLibrary, library_id) is None
        project = await session.get(Project, uuid.UUID(project_id))
        assert project is not None and project.name == TOPIC_NAME


# ---- 2. 课题正好有一个库：沿用 ----


async def test_single_linked_library_is_reused_and_keeps_its_papers(client, live_queue, literature):
    headers = await _login(client)
    project_id = await _topic(client, headers)
    old_titles = ["An older paper already in the library", "Another older paper"]
    own_definition = {
        "statement": "我自己写的库陈述",
        "cadence": "daily",
        "keywords": {"include": ["my own keyword"], "exclude": ["basket"], "sources": ["arxiv"]},
    }
    async with get_sessionmaker()() as session:
        library = await _library(
            session, name="我原来的库", papers=old_titles, definition=dict(own_definition)
        )
        library_id = library.id
        await _link(session, project_id, [library])

    resp = await client.post(
        "/api/research",
        json={"project_id": project_id, "direction": DIRECTION, "client_key": "k-reuse"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    await live_queue.drain()

    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["library_id"] == str(library_id) and detail["name"] == TOPIC_NAME
    assert detail["survey"]["state"] == "done", detail
    row = await _run_row(run_id)
    assert row.phases["plan"]["reused_library"] is True
    assert row.phases["plan"]["existing_project"] is True
    assert await _link_ids(project_id) == {str(library_id)}
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(DirectionLibrary))).scalars().all()) == 1
        library = await session.get(DirectionLibrary, library_id)
        # 库自己配过的一项都没被简报覆盖；名字也没动
        assert library.name == "我原来的库"
        assert library.definition["statement"] == "我自己写的库陈述"
        assert library.definition["cadence"] == "daily"
        assert library.definition["keywords"] == own_definition["keywords"]
        titles = (
            (
                await session.execute(
                    select(Paper.title)
                    .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                    .where(LibraryPaper.library_id == library_id)
                )
            )
            .scalars()
            .all()
        )
        assert set(old_titles) <= set(titles)  # 原来的论文还在
        new_titles = [t for t in titles if t.startswith("Verifiable Agent Workflow Study")]
        assert len(new_titles) == 6  # 检索到的加进了同一个库
        survey = await session.get(VoyageRun, uuid.UUID(detail["survey"]["voyage_id"]))
        # 这次检索按简报的范围搜，不是按库原来的关键词
        assert survey.checkpoint["params"]["query_terms"] == detail["brief"]["include_keywords"]
        assert survey.library_id == library_id

    # 连带删除：沿用的库和原来的课题都留着，论文也在
    resp = await client.delete(f"/api/research/{run_id}?purge=true", headers=headers)
    assert resp.status_code == 204
    async with get_sessionmaker()() as session:
        assert await session.get(Project, uuid.UUID(project_id)) is not None
        assert await session.get(DirectionLibrary, library_id) is not None
        members = await session.execute(
            select(LibraryPaper).where(LibraryPaper.library_id == library_id)
        )
        assert len(members.scalars().all()) == len(titles)
    assert await _link_ids(project_id) == {str(library_id)}


async def test_reused_library_without_search_settings_gets_them_from_the_brief(client, queue_stub):
    """沿用的库什么检索配置都没有（老库）：补上关键词、分类、来源和陈述，检索才有东西
    可用；库回指课题但没建关联的，也补上关联。"""
    headers = await _login(client)
    project_id = await _topic(client, headers)
    async with get_sessionmaker()() as session:
        library = await _library(session, name="老库", origin=uuid.UUID(project_id))
        library_id = library.id
        await session.commit()
    assert await _link_ids(project_id) == set()

    resp = await client.post("/api/research", json={"project_id": project_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    row = await _advance(resp.json()["id"], queue_stub)
    assert row.library_id == library_id and row.phases["plan"]["reused_library"] is True
    assert await _link_ids(project_id) == {str(library_id)}
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        keywords = library.definition["keywords"]
        assert keywords["include"] == row.brief["include_keywords"]
        assert keywords["arxiv_categories"] == row.brief["arxiv_categories"]
        assert keywords["sources"] == row.brief["sources"]
        assert library.definition["statement"] == row.brief["statement"]
        assert "cadence" not in library.definition  # 没配的节奏不替它定


# ---- 3. 课题有不止一个库：新建一个，加进关联 ----


async def test_topic_with_two_libraries_gets_a_new_one_linked_alongside(client, queue_stub):
    headers = await _login(client)
    project_id = await _topic(client, headers)
    async with get_sessionmaker()() as session:
        first = await _library(session, name="库甲", papers=["Paper in A"])
        second = await _library(session, name="库乙", papers=["Paper in B"])
        old_ids = [str(first.id), str(second.id)]
        await _link(session, project_id, [first, second])

    resp = await client.post(
        "/api/research", json={"project_id": project_id, "direction": DIRECTION}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    row = await _advance(resp.json()["id"], queue_stub)
    assert row.phase == "survey"
    assert str(row.library_id) not in old_ids
    assert "reused_library" not in row.phases["plan"]
    assert await _link_ids(project_id) == {*old_ids, str(row.library_id)}  # 原来的两条还在
    async with get_sessionmaker()() as session:
        new = await session.get(DirectionLibrary, row.library_id)
        assert new.name == TOPIC_NAME and str(new.project_id) == project_id
        assert new.cadence == "manual"
        assert new.definition["keywords"]["include"] == row.brief["include_keywords"]

    # 课题已经有一个回指它的库、另外还关联了一个：新库不能再回指（一个课题只有一个）
    other_id = await _topic(client, headers, name="另一个课题", statement=None)
    async with get_sessionmaker()() as session:
        origin = await _library(session, name="回指的库", origin=uuid.UUID(other_id))
        linked = await _library(session, name="关联的库")
        origin_id, linked_id = origin.id, linked.id
        await _link(session, other_id, [linked])
    resp = await client.post("/api/research", json={"project_id": other_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    row = await _advance(resp.json()["id"], queue_stub)
    assert row.library_id not in (origin_id, linked_id)
    assert await _link_ids(other_id) == {str(linked_id), str(row.library_id)}
    async with get_sessionmaker()() as session:
        assert (await session.get(DirectionLibrary, row.library_id)).project_id is None
        assert str((await session.get(DirectionLibrary, origin_id)).project_id) == other_id

    # 连带删除：只删这次研究新建的那个库
    resp = await client.delete(f"/api/research/{row.id}?purge=true", headers=headers)
    assert resp.status_code == 204
    async with get_sessionmaker()() as session:
        assert await session.get(DirectionLibrary, row.library_id) is None
        assert await session.get(DirectionLibrary, origin_id) is not None
        assert await session.get(DirectionLibrary, linked_id) is not None
        assert await session.get(Project, uuid.UUID(other_id)) is not None
    assert await _link_ids(other_id) == {str(linked_id)}


# ---- 4. 方向不填 ----


async def test_direction_defaults_to_topic_statement_then_name(client, queue_stub):
    headers = await _login(client)
    with_statement = await _topic(client, headers)
    resp = await client.post("/api/research", json={"project_id": with_statement}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["direction"] == TOPIC_STATEMENT and resp.json()["name"] == TOPIC_NAME

    name_only = await _topic(client, headers, name="只有名字的课题", statement=None)
    resp = await client.post(
        "/api/research", json={"project_id": name_only, "direction": "   "}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["direction"] == "只有名字的课题"

    # 不带 project_id 时方向仍然必填
    for body in ({}, {"direction": "  "}, {"client_key": "k-none"}):
        resp = await client.post("/api/research", json=body, headers=headers)
        assert resp.status_code == 422 and resp.json()["detail"] == "DIRECTION_INVALID", body
    # 带了 project_id、方向给了但太长：照样 422
    third = await _topic(client, headers, name="第三个课题", statement=None)
    resp = await client.post(
        "/api/research", json={"project_id": third, "direction": "x" * 2001}, headers=headers
    )
    assert resp.status_code == 422 and resp.json()["detail"] == "DIRECTION_INVALID"
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(ResearchRun))).scalars().all()) == 2
        assert len((await session.execute(select(Project))).scalars().all()) == 3


# ---- 5. 研究的文献检索不生成简报和趋势 ----


async def test_research_survey_skips_digest_and_trends_and_still_reaches_choose(
    client, live_queue, literature
):
    headers = await _login(client)
    project_id = await _topic(client, headers)
    resp = await client.post(
        "/api/research", json={"project_id": project_id, "direction": DIRECTION}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    await live_queue.drain()

    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["phase"] == "choose" and detail["status"] == "waiting_user", detail
    assert detail["name"] == TOPIC_NAME and detail["ideas"]
    library_id = uuid.UUID(detail["library_id"])
    async with get_sessionmaker()() as session:
        survey = await session.get(VoyageRun, uuid.UUID(detail["survey"]["voyage_id"]))
        assert survey.status == "done"
        assert survey.checkpoint["params"]["skip_digest"] is True
        assert await _step_actions(session, survey.id) == RESEARCH_PLAN
        digests = await session.execute(
            select(LibraryResearchDigest).where(LibraryResearchDigest.library_id == library_id)
        )
        assert digests.scalars().all() == []
        library = await session.get(DirectionLibrary, library_id)
        # 没有简报也照常记了同步进度
        assert library.ingest_state["last_run"]["voyage_id"] == str(survey.id)

        # 之后想看简报：单独生成，不依赖检索时生成过
        digest_run, strategy, count = await ingest_service.create_digest_voyage(
            session, library=library, created_by=survey.created_by
        )
        digest_run_id = digest_run.id
        assert strategy == "digest_only" and count == 6
    await live_queue.enqueue("run_voyage", str(digest_run_id))
    await live_queue.drain()
    async with get_sessionmaker()() as session:
        assert (await session.get(VoyageRun, digest_run_id)).status == "done"
        assert await _step_actions(session, digest_run_id) == [
            "wiki.daily_digest",
            "wiki.trend_synthesize",
        ]
        digest = (
            await session.execute(
                select(LibraryResearchDigest).where(LibraryResearchDigest.library_id == library_id)
            )
        ).scalar_one()
        assert digest.trend_model is not None


async def test_plain_library_search_still_has_all_eight_steps(client):
    """不带 skip_digest 的文献库检索：计划和任务参数都和以前一样。"""
    await _login(client)
    async with get_sessionmaker()() as session:
        library = await _library(session, name="普通的库")
        plain = await ingest_service.create_ingest_voyage(
            session, library=library, mode="search", knobs=IngestKnobs(), created_by=None
        )
        assert list(plain.checkpoint["params"]) == ["mode", "knobs", "query_terms", "time_range"]
        assert [step["action"] for step in wiki_plan(plain)] == FULL_PLAN
        plain.status = "cancelled"  # 让出这个库，下面再建一条
        await session.commit()
        skipping = await ingest_service.create_ingest_voyage(
            session,
            library=library,
            mode="search",
            knobs=IngestKnobs(),
            created_by=None,
            skip_digest=True,
        )
        assert skipping.checkpoint["params"]["skip_digest"] is True
        # 只跳过简报和趋势；沿引用扩展那一步是调研检索（strategy=survey）才有的
        assert [step["action"] for step in wiki_plan(skipping)] == [
            a for a in RESEARCH_PLAN if a != "wiki.expand_citations"
        ]
