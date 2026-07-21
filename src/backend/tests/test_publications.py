"""我发表的论文（issue #109）：绑定/候选、同步产候选与去重、确认流、手动补录，全离线。"""

import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx

from app.core.db import get_sessionmaker
from app.services import publications as publications_service
from app.services.literature import reset_clients, set_clients
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import register_and_login


@pytest_asyncio.fixture
async def lit_clients():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(openalex=OpenAlexClient(redis=redis, mailto="test@example.org"))
    yield
    reset_clients()
    await redis.aclose()


def _author(idx: int, name: str, inst: str) -> dict:
    return {
        "id": f"https://openalex.org/A{idx}",
        "display_name": name,
        "display_name_alternatives": [f"{name[0]}. {name.split()[-1]}"],
        "works_count": 40 + idx,
        "cited_by_count": 1000 + idx,
        "affiliations": [{"institution": {"display_name": inst}, "years": [2024]}],
        "ids": {"openalex": f"https://openalex.org/A{idx}", "orcid": None},
    }


def _work(idx: int, title: str, *, doi: str | None = None, cites: int = 10) -> dict:
    return {
        "id": f"https://openalex.org/W{idx}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "publication_year": 2024,
        "publication_date": "2024-05-01",
        "cited_by_count": cites,
        "primary_location": {
            "landing_page_url": f"https://example.org/{idx}",
            "source": {"display_name": "NeurIPS"},
        },
        "authorships": [
            {
                "author": {"display_name": "Wei Zhang"},
                "institutions": [{"display_name": "Zhejiang University"}],
            }
        ],
    }


async def _login(client):
    token = await register_and_login(client)
    return {"Authorization": f"Bearer {token}"}


async def test_profile_bind_roundtrip(client):
    headers = await _login(client)
    resp = await client.get("/api/me/author-profile", headers=headers)
    assert resp.status_code == 404

    resp = await client.put(
        "/api/me/author-profile",
        json={
            "name_variants": ["Wei Zhang", "W. Zhang", "Wei Zhang", " "],
            "affiliations": ["Zhejiang University"],
            "openalex_author_id": "A123",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name_variants"] == ["Wei Zhang", "W. Zhang"]  # 去重去空
    assert body["openalex_author_id"] == "A123"
    assert body["auto_sync"] is True

    resp = await client.get("/api/me/author-profile", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["affiliations"] == ["Zhejiang University"]


@respx.mock
async def test_author_candidates_affiliation_ranking(client, lit_clients):
    headers = await _login(client)
    respx.get(url__regex=r"https://api\.openalex\.org/authors.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _author(1, "Wei Zhang", "MIT"),
                    _author(2, "Wei Zhang", "Zhejiang University"),
                ]
            },
        )
    )
    resp = await client.get(
        "/api/me/author-profile/candidates?name=Wei+Zhang&affiliation=zhejiang",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    cands = resp.json()
    assert [c["openalex_author_id"] for c in cands] == ["A2", "A1"]  # 机构命中的排前
    assert cands[0]["affiliations"] == ["Zhejiang University"]
    assert cands[0]["works_count"] == 42


@respx.mock
async def test_sync_creates_pending_and_skips_seen(client, lit_clients):
    headers = await _login(client)
    await client.put(
        "/api/me/author-profile",
        json={"name_variants": ["Wei Zhang"], "affiliations": [], "openalex_author_id": "A2"},
        headers=headers,
    )
    works_route = respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _work(1, "Paper One", doi="10.1/one", cites=50),
                    _work(2, "Paper Two", doi="10.48550/arXiv.2401.00002"),
                ],
                "meta": {"next_cursor": None},
            },
        )
    )
    me = (await client.get("/api/users/me", headers=headers)).json()
    async with get_sessionmaker()() as session:
        added = await publications_service.sync_publications(session, user_id=uuid.UUID(me["id"]))
    assert added == 2
    assert works_route.called

    resp = await client.get("/api/me/publications?status=pending", headers=headers)
    body = resp.json()
    assert body["total"] == 2
    assert body["counts"] == {"pending": 2, "confirmed": 0, "rejected": 0}
    by_title = {p["title"]: p for p in body["items"]}
    assert by_title["Paper Two"]["arxiv_id"] == "2401.00002"  # DataCite DOI 反推 arxiv id
    assert by_title["Paper One"]["cited_by_count"] == 50

    # 驳回一篇后再次同步：不重复产候选
    reject_id = by_title["Paper One"]["id"]
    resp = await client.post(f"/api/me/publications/{reject_id}/reject", headers=headers)
    assert resp.json()["status"] == "rejected"
    async with get_sessionmaker()() as session:
        added = await publications_service.sync_publications(session, user_id=uuid.UUID(me["id"]))
    assert added == 0
    resp = await client.get("/api/me/publications?status=pending", headers=headers)
    assert resp.json()["total"] == 1


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
    other = {
        "Authorization": f"Bearer {await register_and_login(client, email='other@example.com')}"
    }
    resp = await client.get("/api/me/publications?status=confirmed", headers=other)
    assert resp.json()["total"] == 0
    resp = await client.post(f"/api/me/publications/{pub['id']}/confirm", headers=other)
    assert resp.status_code == 404


async def test_sync_endpoint_requires_binding_and_enqueues(client, queue_stub):
    headers = await _login(client)
    resp = await client.post("/api/me/publications/sync", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "AUTHOR_NOT_BOUND"

    await client.put(
        "/api/me/author-profile",
        json={"name_variants": ["Wei Zhang"], "affiliations": [], "openalex_author_id": "A2"},
        headers=headers,
    )
    resp = await client.post("/api/me/publications/sync", headers=headers)
    assert resp.status_code == 202
    assert queue_stub.jobs and queue_stub.jobs[0][0] == "sync_user_publications"
