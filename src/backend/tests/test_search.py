"""全局搜索（顶栏 ⌘K）：GET /projects/{project_id}/search。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.manuscript import Manuscript
from app.models.voyage import VoyageRun
from tests.conftest import add_concept, add_paper

from .conftest import register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "search-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    pid = uuid.UUID(project_id)

    async with get_sessionmaker()() as session:
        idea = Idea(project_id=pid, title="Graph retrieval idea", summary="用图检索增强 RAG")
        graph_paper = await add_paper(
            session,
            project_id=pid,
            title="Graph Retrieval for LLMs",
            tldr="graph-based retrieval",
            status="included",
        )
        session.add_all(
            [
                graph_paper,
                await add_paper(
                    session,
                    project_id=pid,
                    title="Excluded graph paper",
                    status="excluded",
                ),
                # 概念按「库内论文关联到它」进课题作用域（概念本身不属于任何库）
                await add_concept(
                    session,
                    project_id=pid,
                    paper_id=graph_paper.id,
                    name="Graph RAG",
                    slug="graph-rag",
                    definition="图增强检索",
                ),
                idea,
                VoyageRun(project_id=pid, kind="wiki_bootstrap", goal="graph literature survey"),
                Manuscript(project_id=pid, title="Graph Retrieval Paper Draft"),
            ]
        )
        await session.flush()
        session.add(Experiment(project_id=pid, idea_id=idea.id))
        await session.commit()
    return project_id, headers


async def test_search_across_entities(client):
    project_id, headers = await _setup(client)

    resp = await client.get(
        f"/api/projects/{project_id}/global-search", params={"q": "graph"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "graph"
    by_type = {}
    for hit in body["hits"]:
        by_type.setdefault(hit["type"], []).append(hit)

    assert [h["title"] for h in by_type["paper"]] == ["Graph Retrieval for LLMs"]  # excluded 不出现
    assert by_type["concept"][0]["title"] == "Graph RAG"
    assert by_type["idea"][0]["title"] == "Graph retrieval idea"
    assert by_type["experiment"][0]["title"] == "Graph retrieval idea"  # 实验用想法标题
    assert by_type["voyage"][0]["snippet"] == "wiki_bootstrap"
    assert by_type["manuscript"][0]["title"] == "Graph Retrieval Paper Draft"


async def test_search_matches_are_case_insensitive_and_scoped(client):
    project_id, headers = await _setup(client)

    resp = await client.get(
        f"/api/projects/{project_id}/global-search",
        params={"q": "GRAPH RETRIEVAL FOR"},
        headers=headers,
    )
    assert resp.status_code == 200
    types = {h["type"] for h in resp.json()["hits"]}
    assert types == {"paper"}

    resp = await client.get(
        f"/api/projects/{project_id}/global-search", params={"q": "不存在的关键词"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["hits"] == []


async def test_search_requires_membership(client):
    project_id, _ = await _setup(client)
    other = await register_and_login(client, email="bob@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/global-search",
        params={"q": "graph"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


async def test_search_skips_the_recycle_bin(client):
    """回收站里的想法/实验/稿件不得被搜出来。

    这条原则代码里早就定了（见 test_search_excludes_trash.py：论文那半边），只是全局
    搜索这条路一直没跟上。列表页看不见、搜索却照样捞出来，点进去还是活的——用户会
    以为删除压根没生效。稿件和实验同样是软删的，一起钉住，免得下次只修一种。
    """
    import datetime as dt

    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)

    async def types_for(q: str) -> set[str]:
        r = await client.get(
            f"/api/projects/{project_id}/global-search", params={"q": q}, headers=headers
        )
        assert r.status_code == 200, r.text
        return {hit["type"] for hit in r.json()["hits"]}

    before = await types_for("graph")
    assert {"idea", "experiment", "manuscript"} <= before, "先确认这三类本来搜得到"

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        idea = (await session.execute(select(Idea).where(Idea.project_id == pid))).scalar_one()
        idea.trashed_at = now
        manuscript = (
            await session.execute(select(Manuscript).where(Manuscript.project_id == pid))
        ).scalar_one()
        manuscript.trashed_at = now
        experiment = (
            await session.execute(select(Experiment).where(Experiment.project_id == pid))
        ).scalar_one()
        experiment.trashed_at = now
        await session.commit()

    after = await types_for("graph")
    assert "idea" not in after, "回收站里的想法仍被搜出来"
    assert "manuscript" not in after, "回收站里的稿件仍被搜出来"
    assert "experiment" not in after, "回收站里的实验仍被搜出来"
    # 没有回收站概念的类型不受影响
    assert "paper" in after and "voyage" in after
