"""管理员文献检索设置：持久化、脱敏、校验和权限。"""

import httpx

from app.core.db import get_sessionmaker
from app.schemas.literature_discovery import SourceSearchPage
from app.services import literature_settings
from tests.conftest import register_and_login


async def _admin_and_member(client):
    admin = await register_and_login(client, email="lit-admin@example.com")
    member = await register_and_login(client, email="lit-member@example.com")
    return {"Authorization": f"Bearer {admin}"}, {"Authorization": f"Bearer {member}"}


async def test_literature_settings_roundtrip_masks_provider_keys(client):
    admin, member = await _admin_and_member(client)
    response = await client.put(
        "/api/admin/settings/literature-search",
        json={
            "sources": ["openalex", "semantic", "sciverse"],
            "requested_count": 50,
            "candidate_budget": 200,
            "start_year": 2016,
            "end_year": 2026,
            "score_weights": {"relevance": 0.7, "quality": 0.3},
            "provider_keys": {"sciverse": ["sciverse-secret-1234", "sciverse-secret-5678"]},
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested_count"] == 50
    assert payload["provider_keys"]["sciverse"][0]["preview"] == "••••1234"
    assert "sciverse-secret-1234" not in response.text

    response = await client.get("/api/admin/settings/literature-search", headers=admin)
    assert response.status_code == 200
    assert response.json()["provider_keys"]["sciverse"][1]["configured"] is True

    # 管理端点对任何登录用户开放（#614）
    response = await client.get("/api/admin/settings/literature-search", headers=member)
    assert response.status_code == 200


async def test_literature_settings_reject_invalid_source_and_year_window(client):
    admin, _ = await _admin_and_member(client)
    response = await client.put(
        "/api/admin/settings/literature-search",
        json={"sources": ["not-a-provider"]},
        headers=admin,
    )
    assert response.status_code == 422
    assert "INVALID_LITERATURE_SETTING:sources" in response.text

    response = await client.put(
        "/api/admin/settings/literature-search",
        json={"start_year": 2026, "end_year": 2016},
        headers=admin,
    )
    assert response.status_code == 422
    assert "INVALID_LITERATURE_SETTING:year_window" in response.text


async def test_discovery_run_inherits_admin_defaults_without_persisting_keys(client):
    admin, _ = await _admin_and_member(client)
    response = await client.put(
        "/api/admin/settings/literature-search",
        json={
            "sources": ["pubmed", "core"],
            "requested_count": 50,
            "candidate_budget": 150,
            "start_year": 2016,
            "end_year": 2025,
            "score_weights": {"relevance": 0.8, "quality": 0.2},
            "provider_keys": {"pubmed": ["private-pubmed-key"]},
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    library = await client.post(
        "/api/libraries",
        json={"name": "Admin defaults", "statement": "Runtime wiring"},
        headers=admin,
    )
    assert library.status_code == 201, library.text

    response = await client.post(
        f"/api/libraries/{library.json()['id']}/literature/runs",
        json={
            "topic": "structural impact response",
            "source_config": {"provider_keys": {"pubmed": ["request-injected-secret"]}},
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["requested_count"] == 50
    assert run["candidate_budget"] == 150
    assert run["start_year"] == 2016
    assert run["end_year"] == 2025
    assert run["source_config"] == {
        "sources": ["pubmed", "core"],
        "score_weights": {
            "relevance": 0.8,
            "evidence_quality": 0.2,
            "impact": 0.0,
            "novelty": 0.0,
            "recency": 0.0,
        },
    }
    assert [attempt["source"] for attempt in run["source_attempts"]] == ["core", "pubmed"]
    assert "private-pubmed-key" not in response.text
    assert "request-injected-secret" not in response.text


async def test_provider_credential_crud_has_stable_id_and_runtime_enable_gate(client):
    admin, member = await _admin_and_member(client)
    response = await client.post(
        "/api/admin/settings/literature-search/credentials",
        json={
            "source": "openalex",
            "secret": "openalex-secret-1234",
            "label": "primary",
            "enabled": True,
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    credential_id = created["id"]
    assert created["source"] == "openalex"
    assert created["preview"] == "••••1234"
    assert created["label"] == "primary"
    assert "openalex-secret-1234" not in response.text

    response = await client.patch(
        f"/api/admin/settings/literature-search/credentials/{credential_id}",
        json={"secret": "replacement-secret-5678", "label": "secondary", "enabled": False},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == credential_id
    assert response.json()["preview"] == "••••5678"
    assert response.json()["enabled"] is False

    async with get_sessionmaker()() as session:
        runtime = await literature_settings.get_runtime_settings(session)
    assert runtime["provider_keys"]["openalex"] == []

    response = await client.patch(
        f"/api/admin/settings/literature-search/credentials/{credential_id}",
        json={"enabled": True, "label": None},
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["label"] is None
    async with get_sessionmaker()() as session:
        runtime = await literature_settings.get_runtime_settings(session)
    assert runtime["provider_keys"]["openalex"] == ["replacement-secret-5678"]

    # 任何登录用户都能删（#614）
    response = await client.delete(
        f"/api/admin/settings/literature-search/credentials/{credential_id}", headers=member
    )
    assert response.status_code == 204
    response = await client.delete(
        f"/api/admin/settings/literature-search/credentials/{credential_id}", headers=admin
    )
    assert response.status_code == 404


async def test_arxiv_rejects_credentials_because_public_api_needs_no_key(client):
    admin, _ = await _admin_and_member(client)
    response = await client.post(
        "/api/admin/settings/literature-search/credentials",
        json={"source": "arxiv", "secret": "unnecessary-key"},
        headers=admin,
    )
    assert response.status_code == 422
    assert "INVALID_LITERATURE_CREDENTIAL:source" in response.text


async def test_easyscholar_credential_uses_metric_probe(client, monkeypatch):
    admin, _ = await _admin_and_member(client)
    response = await client.post(
        "/api/admin/settings/literature-search/credentials",
        json={"source": "easyscholar", "secret": "easyscholar-secret"},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    credential_id = response.json()["id"]
    observed = {}

    async def fake_probe(settings, *, source, venue_name):
        observed.update(settings=settings, source=source, venue_name=venue_name)
        return True

    monkeypatch.setattr(
        "app.services.literature.venue_metrics.probe_venue_metric_provider", fake_probe
    )
    response = await client.post(
        f"/api/admin/settings/literature-search/credentials/{credential_id}/test",
        json={"query": "Journal of Tests"},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["fetched_count"] == 1
    assert observed["source"] == "easyscholar"
    assert observed["venue_name"] == "Journal of Tests"
    assert observed["settings"]["provider_keys"] == {
        "easyscholar": ["easyscholar-secret"]
    }


async def test_unpaywall_health_probe_uses_doi_resolver_without_adding_search_source(
    client, monkeypatch
):
    admin, _ = await _admin_and_member(client)
    observed = {}

    class Resolver:
        async def lookup_unpaywall(self, doi):
            observed["doi"] = doi
            return {"doi": doi, "best_oa_location": {"url_for_pdf": "https://example.test/a.pdf"}}

        async def aclose(self):
            observed["closed"] = True

    monkeypatch.setattr("app.services.literature.multi_source.MultiSourceClient", Resolver)
    response = await client.post(
        "/api/admin/settings/literature-search/test",
        json={"source": "unpaywall", "query": "10.1000/test-doi"},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["fetched_count"] == 1
    assert observed == {"doi": "10.1000/test-doi", "closed": True}
    settings = await client.get("/api/admin/settings/literature-search", headers=admin)
    assert "unpaywall" not in settings.json()["sources"]
    assert settings.json()["provider_health"]["unpaywall"]["ok"] is True


async def test_single_credential_probe_updates_only_that_entry(client, monkeypatch):
    admin, _ = await _admin_and_member(client)
    response = await client.post(
        "/api/admin/settings/literature-search/credentials",
        json={"source": "semantic", "secret": "semantic-key-under-test"},
        headers=admin,
    )
    credential_id = response.json()["id"]
    observed = {}

    class Adapter:
        async def search(self, request):
            observed["query"] = request.query
            return SourceSearchPage(source="semantic", fetched_count=1)

    class Registry:
        def get(self, source):
            observed["source"] = source
            return Adapter()

    async def fake_registry(settings):
        observed["keys"] = settings["provider_keys"]
        return Registry()

    from app.services.literature import runtime

    monkeypatch.setattr(runtime, "build_adapter_registry", fake_registry)
    response = await client.post(
        f"/api/admin/settings/literature-search/credentials/{credential_id}/test",
        json={"query": "impact response"},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert observed == {
        "keys": {"semantic": ["semantic-key-under-test"]},
        "source": "semantic",
        "query": "impact response",
    }
    response = await client.get("/api/admin/settings/literature-search", headers=admin)
    item = response.json()["provider_keys"]["semantic"][0]
    assert item["id"] == credential_id
    assert item["health"]["ok"] is True


async def test_credential_probe_never_returns_or_persists_secret_url(client, monkeypatch):
    admin, _ = await _admin_and_member(client)
    secret = "SECRET_SENTINEL"
    response = await client.post(
        "/api/admin/settings/literature-search/credentials",
        json={"source": "openalex", "secret": secret},
        headers=admin,
    )
    credential_id = response.json()["id"]

    class Adapter:
        async def search(self, request):
            del request
            request = httpx.Request(
                "GET", f"https://api.openalex.org/works?api_key={secret}&search=x"
            )
            upstream = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=upstream)

    class Registry:
        def get(self, source):
            assert source == "openalex"
            return Adapter()

    async def fake_registry(settings):
        assert settings["provider_keys"] == {"openalex": [secret]}
        return Registry()

    from app.services.literature import runtime

    monkeypatch.setattr(runtime, "build_adapter_registry", fake_registry)
    response = await client.post(
        f"/api/admin/settings/literature-search/credentials/{credential_id}/test",
        json={"query": "impact response"},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert response.json()["detail"] == "openalex provider request failed (HTTP_401)"
    assert secret not in response.text
    saved = await client.get("/api/admin/settings/literature-search", headers=admin)
    assert secret not in saved.text
    assert (
        saved.json()["provider_keys"]["openalex"][0]["health"]["detail"]
        == response.json()["detail"]
    )
