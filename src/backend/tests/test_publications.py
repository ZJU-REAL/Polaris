"""我发表的论文（issue #109）：绑定、文献库姓名+机构匹配、确认流、手动补录，全离线。"""

import uuid

from app.core.db import get_sessionmaker
from app.services import publications as publications_service
from tests.conftest import add_paper, register_and_login


async def _login(client, email="alice@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _make_project(client, headers, name="pub-proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    return resp.json()["id"]


async def _make_paper(project_id: str, **kwargs) -> str:
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), **kwargs)
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _bind(client, headers, *, names=None, affils=None):
    resp = await client.put(
        "/api/me/author-profile",
        json={"name_variants": names or ["Wei Zhang"], "affiliations": affils or []},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _my_id(client, headers) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/users/me", headers=headers)).json()["id"])


async def test_profile_bind_roundtrip(client):
    headers = await _login(client)
    resp = await client.get("/api/me/author-profile", headers=headers)
    assert resp.status_code == 404

    body = await _bind(
        client,
        headers,
        names=["Wei Zhang", "W. Zhang", "Wei Zhang", " "],
        affils=["Zhejiang University"],
    )
    assert body["name_variants"] == ["Wei Zhang", "W. Zhang"]  # 去重去空
    assert body["auto_sync"] is True

    resp = await client.get("/api/me/author-profile", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["affiliations"] == ["Zhejiang University"]


async def test_library_match_name_and_affiliation(client):
    headers = await _login(client)
    project_id = await _make_project(client, headers)
    # 命中：姓名词序颠倒 + 机构包含匹配
    hit = await _make_paper(
        project_id,
        title="Hit Paper",
        arxiv_id="2401.00001",
        authors=[{"name": "Zhang Wei"}, {"name": "Alice Smith"}],
        affiliations=["College of CS, Zhejiang University", "MIT"],
        status="included",
    )
    # 不命中：姓名不匹配
    await _make_paper(
        project_id, title="Other Author", authors=[{"name": "Bob Jones"}], status="included"
    )
    # 不命中：姓名命中但机构冲突（论文有机构信息且无交集）
    await _make_paper(
        project_id,
        title="Wrong Affiliation",
        authors=[{"name": "Wei Zhang"}],
        affiliations=["Stanford University"],
        status="included",
    )
    # 命中：姓名命中、论文无机构信息（未 enrich 不设门槛，交给人工确认）
    await _make_paper(
        project_id, title="No Affiliation Info", authors=[{"name": "Wei Zhang"}], status="scored"
    )
    # 不命中：回收站论文
    await _make_paper(
        project_id, title="Trashed", authors=[{"name": "Wei Zhang"}], status="excluded"
    )
    await _bind(client, headers, names=["Wei Zhang"], affils=["Zhejiang University"])

    async with get_sessionmaker()() as session:
        added = await publications_service.match_from_library(
            session, user_id=await _my_id(client, headers)
        )
    assert added == 2

    resp = await client.get("/api/me/publications?status=pending", headers=headers)
    body = resp.json()
    titles = {p["title"] for p in body["items"]}
    assert titles == {"Hit Paper", "No Affiliation Info"}
    by_title = {p["title"]: p for p in body["items"]}
    assert by_title["Hit Paper"]["paper_id"] == hit  # 可跳回文献库论文
    assert by_title["Hit Paper"]["source"] == "library"


async def test_match_idempotent_and_respects_rejected(client):
    headers = await _login(client)
    project_id = await _make_project(client, headers)
    await _make_paper(
        project_id,
        title="Same Paper",
        arxiv_id="2402.11111",
        authors=[{"name": "Wei Zhang"}],
        status="included",
    )
    await _bind(client, headers)
    uid = await _my_id(client, headers)

    async with get_sessionmaker()() as session:
        assert await publications_service.match_from_library(session, user_id=uid) == 1
    # 重复跑：去重键幂等
    async with get_sessionmaker()() as session:
        assert await publications_service.match_from_library(session, user_id=uid) == 0

    # 驳回后再跑：不再打扰
    resp = await client.get("/api/me/publications?status=pending", headers=headers)
    pub_id = resp.json()["items"][0]["id"]
    resp = await client.post(f"/api/me/publications/{pub_id}/reject", headers=headers)
    assert resp.json()["status"] == "rejected"
    async with get_sessionmaker()() as session:
        assert await publications_service.match_from_library(session, user_id=uid) == 0
    resp = await client.get("/api/me/publications?status=pending", headers=headers)
    assert resp.json()["total"] == 0


async def test_match_only_scans_member_projects(client):
    headers_a = await _login(client, email="owner@example.com")
    project_a = await _make_project(client, headers_a, "theirs")
    await _make_paper(
        project_a, title="Their Paper", authors=[{"name": "Wei Zhang"}], status="included"
    )

    headers_b = await _login(client, email="me@example.com")
    await _bind(client, headers_b)
    uid = await _my_id(client, headers_b)
    async with get_sessionmaker()() as session:
        assert await publications_service.match_from_library(session, user_id=uid) == 0


async def test_daily_match_targets_auto_sync_profiles(client):
    headers = await _login(client)
    await _bind(client, headers)
    uid = await _my_id(client, headers)
    async with get_sessionmaker()() as session:
        assert await publications_service.profiles_for_daily_match(session) == [uid]

    # 关掉自动匹配后不在每日名单里
    resp = await client.put(
        "/api/me/author-profile",
        json={"name_variants": ["Wei Zhang"], "affiliations": [], "auto_sync": False},
        headers=headers,
    )
    assert resp.json()["auto_sync"] is False
    async with get_sessionmaker()() as session:
        assert await publications_service.profiles_for_daily_match(session) == []


async def test_confirm_flow_and_permissions(client):
    headers = await _login(client)
    # 手动补录 bibtex（离线）→ 直接 confirmed
    bibtex = """@inproceedings{zhang2025agents,
      title = {Agents that Publish},
      author = {Zhang, Wei and Alice Smith},
      year = {2025},
      booktitle = {Proceedings of ICML},
      doi = {10.9/agents},
    }"""
    resp = await client.post("/api/me/publications", json={"bibtex": bibtex}, headers=headers)
    assert resp.status_code == 201, resp.text
    pub = resp.json()
    assert pub["status"] == "confirmed"
    assert pub["source"] == "manual"
    assert pub["venue"] == "Proceedings of ICML"

    # 同一篇再次补录：不重复建行
    resp = await client.post("/api/me/publications", json={"bibtex": bibtex}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["id"] == pub["id"]

    # 三选一校验
    resp = await client.post(
        "/api/me/publications", json={"doi": "10.9/x", "arxiv_id": "2401.1"}, headers=headers
    )
    assert resp.status_code == 422

    # 别人看不到、也改不动我的记录
    other = await _login(client, email="other@example.com")
    resp = await client.get("/api/me/publications?status=confirmed", headers=other)
    assert resp.json()["total"] == 0
    resp = await client.post(f"/api/me/publications/{pub['id']}/confirm", headers=other)
    assert resp.status_code == 404


async def test_scan_endpoint_requires_binding_and_enqueues(client, queue_stub):
    headers = await _login(client)
    resp = await client.post("/api/me/publications/sync", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "AUTHOR_NOT_BOUND"

    await _bind(client, headers)
    resp = await client.post("/api/me/publications/sync", headers=headers)
    assert resp.status_code == 202
    assert queue_stub.jobs and queue_stub.jobs[0][0] == "match_user_publications"
