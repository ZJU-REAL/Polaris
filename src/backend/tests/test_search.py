"""全局搜索（顶栏 ⌘K）：GET /projects/{project_id}/search。"""

import uuid

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
        session.add_all(
            [
                await add_paper(session,
                    project_id=pid,
                    title="Graph Retrieval for LLMs",
                    tldr="graph-based retrieval",
                    status="included",
                ),
                await add_paper(
                    session,
                    project_id=pid,
                    title="Excluded graph paper",
                    status="excluded",
                ),
                await add_concept(session,
                    project_id=pid, name="Graph RAG", slug="graph-rag", definition="图增强检索"
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
