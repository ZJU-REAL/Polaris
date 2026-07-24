"""个人库引用导出（GET /me/library/export/citations）+ 语义检索回退。

覆盖：全部收藏导出、ids 子集导出、非本人 / 非收藏 / 浏览记录不含、csl-json、非法 format→422、
非 postgres（sqlite）下 mode=semantic 回退 keyword。
"""

import json
import uuid

from app.core.db import get_sessionmaker
from tests.conftest import add_paper, register_and_login


async def _make_project(client, headers, name="pexport-proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    return resp.json()["id"]


async def _make_paper(project_id: str, **kwargs) -> str:
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), **kwargs)
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _save(client, headers, paper_id: str) -> None:
    resp = await client.post("/api/me/library", json={"paper_id": paper_id}, headers=headers)
    assert resp.status_code == 201, resp.text


async def test_personal_export_all_saved(client):
    token = await register_and_login(client, email="pexport-all@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p_conf = await _make_paper(
        project_id,
        title="The Great Agent Benchmark",
        authors=[{"name": "Alice Smith"}],
        year=2024,
        venue="Proceedings of NeurIPS",
        status="included",
    )
    p_arxiv = await _make_paper(
        project_id,
        title="Quantum Annealing Survey",
        authors=[{"name": "张三"}],
        year=2025,
        arxiv_id="2501.00042",
        status="included",
    )
    # 只浏览未收藏的一篇：不应出现在导出里
    p_history = await _make_paper(project_id, title="Only Browsed", year=2020, status="included")
    await client.post("/api/me/library/visits", json={"paper_id": p_history}, headers=headers)

    await _save(client, headers, p_conf)
    await _save(client, headers, p_arxiv)

    resp = await client.get("/api/me/library/export/citations?format=bibtex", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and "polaris-my-library-citations.bib" in disp
    bib = resp.text
    assert "@inproceedings{smith2024great,\n" in bib
    assert "eprint = {2501.00042}," in bib
    assert "Only Browsed" not in bib  # 浏览记录（未收藏）不含


async def test_personal_export_ids_subset(client):
    token = await register_and_login(client, email="pexport-ids@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p1 = await _make_paper(project_id, title="First Saved", year=2022, status="included")
    p2 = await _make_paper(project_id, title="Second Saved", year=2023, status="included")
    await _save(client, headers, p1)
    await _save(client, headers, p2)

    resp = await client.get(
        f"/api/me/library/export/citations?format=bibtex&ids={p1}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert "First Saved" in resp.text
    assert "Second Saved" not in resp.text


async def test_personal_export_excludes_other_users_and_unsaved(client):
    token = await register_and_login(client, email="pexport-owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    mine = await _make_paper(project_id, title="Mine Saved", year=2021, status="included")
    unsaved = await _make_paper(project_id, title="Mine Unsaved", year=2021, status="included")
    await _save(client, headers, mine)
    # unsaved 仅浏览不收藏
    await client.post("/api/me/library/visits", json={"paper_id": unsaved}, headers=headers)

    # 传入本人未收藏的论文 id → 不含
    resp = await client.get(
        f"/api/me/library/export/citations?format=csl-json&ids={unsaved}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.text) == []

    # 传入别人的论文 id（陌生 uuid）→ 不含
    stranger = str(uuid.uuid4())
    resp = await client.get(
        f"/api/me/library/export/citations?format=csl-json&ids={stranger}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.text) == []

    # 另一个用户的个人库导出看不到本人收藏
    other = await register_and_login(client, email="pexport-other@example.com")
    other_headers = {"Authorization": f"Bearer {other}"}
    resp = await client.get(
        "/api/me/library/export/citations?format=bibtex", headers=other_headers
    )
    assert resp.status_code == 200, resp.text
    assert "Mine Saved" not in resp.text


async def test_personal_export_csl_and_invalid_format(client):
    token = await register_and_login(client, email="pexport-csl@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p1 = await _make_paper(project_id, title="CSL Paper", year=2024, status="included")
    await _save(client, headers, p1)

    resp = await client.get("/api/me/library/export/citations?format=csl-json", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "polaris-my-library-citations.json" in resp.headers["content-disposition"]
    titles = {item["title"] for item in json.loads(resp.text)}
    assert titles == {"CSL Paper"}

    resp = await client.get("/api/me/library/export/citations?format=ris", headers=headers)
    assert resp.status_code == 422


async def test_semantic_falls_back_to_keyword_on_sqlite(client):
    """sqlite 不支持 pgvector → mode=semantic 回退 keyword，仍走关键词过滤。"""
    token = await register_and_login(client, email="pexport-sem@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p1 = await _make_paper(
        project_id, title="Attention Is All You Need", year=2017, status="included"
    )
    p2 = await _make_paper(project_id, title="Deep Residual Learning", year=2015, status="included")
    await _save(client, headers, p1)
    await _save(client, headers, p2)

    resp = await client.get(
        "/api/me/library?tab=saved&mode=semantic&q=attention", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode_used"] == "keyword"  # 回退
    assert [i["title"] for i in body["items"]] == ["Attention Is All You Need"]
