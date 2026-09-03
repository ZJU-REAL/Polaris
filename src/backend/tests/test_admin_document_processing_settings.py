"""Administrator document-processing settings and MinerU credential lifecycle."""

import httpx

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.system_setting import SystemSetting
from app.services import document_processing_settings
from tests.conftest import register_and_login


async def _admin_and_member(client):
    admin = await register_and_login(client, email="processing-admin@example.com")
    member = await register_and_login(client, email="processing-member@example.com")
    return {"Authorization": f"Bearer {admin}"}, {"Authorization": f"Bearer {member}"}


async def test_document_processing_settings_roundtrip_and_permissions(client):
    admin, member = await _admin_and_member(client)
    response = await client.put(
        "/api/admin/settings/document-processing",
        json={
            "mineru_enabled": True,
            "mineru_base_url": "https://mineru.example/api/v4/",
            "mineru_timeout_seconds": 7200,
            "mineru_poll_interval_seconds": 15,
            "mineru_retries": 3,
            "mineru_concurrency": 4,
            "pymupdf_fallback_enabled": True,
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mineru_base_url"] == "https://mineru.example/api/v4"
    assert payload["mineru_timeout_seconds"] == 7200
    assert payload["mineru_concurrency"] == 4
    assert payload["mineru_credentials"] == []

    # 管理端点对任何登录用户开放（#614）
    response = await client.get("/api/admin/settings/document-processing", headers=member)
    assert response.status_code == 200


async def test_document_processing_settings_reject_unsafe_policy_and_url(client):
    admin, _member = await _admin_and_member(client)
    response = await client.put(
        "/api/admin/settings/document-processing",
        json={"mineru_enabled": False, "pymupdf_fallback_enabled": False},
        headers=admin,
    )
    assert response.status_code == 422
    assert "INVALID_DOCUMENT_PROCESSING_SETTING:parser_policy" in response.text

    response = await client.put(
        "/api/admin/settings/document-processing",
        json={"mineru_base_url": "https://user:secret@mineru.example/api/v4"},
        headers=admin,
    )
    assert response.status_code == 422
    assert "INVALID_DOCUMENT_PROCESSING_SETTING:mineru_base_url" in response.text


async def test_runtime_keeps_environment_tokens_until_database_pool_is_declared(
    client, monkeypatch
):
    admin, _member = await _admin_and_member(client)
    app_settings = get_settings()
    monkeypatch.setattr(app_settings, "mineru_api_tokens", "env-key-a,env-key-b", raising=False)
    response = await client.put(
        "/api/admin/settings/document-processing",
        json={"mineru_concurrency": 3},
        headers=admin,
    )
    assert response.status_code == 200, response.text

    async with get_sessionmaker()() as session:
        runtime = await document_processing_settings.get_runtime_config(session)

    assert runtime.mineru_api_tokens == ("env-key-a", "env-key-b")


async def test_mineru_credential_crud_is_masked_encrypted_and_controls_runtime(client, monkeypatch):
    admin, _member = await _admin_and_member(client)
    app_settings = get_settings()
    monkeypatch.setattr(app_settings, "mineru_api_tokens", "environment-fallback", raising=False)
    secret = "mineru-secret-1234"
    response = await client.post(
        "/api/admin/settings/document-processing/credentials",
        json={"secret": secret, "label": "primary", "enabled": True},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    credential_id = created["id"]
    assert created["provider"] == "mineru"
    assert created["preview"] == "••••1234"
    assert secret not in response.text

    async with get_sessionmaker()() as session:
        row = await session.get(SystemSetting, document_processing_settings.SETTING_KEY)
        assert row is not None
        assert secret not in str(row.value)
        runtime = await document_processing_settings.get_runtime_config(session)
        assert runtime.mineru_api_tokens == (secret,)

    response = await client.patch(
        f"/api/admin/settings/document-processing/credentials/{credential_id}",
        json={"enabled": False, "label": "disabled"},
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    async with get_sessionmaker()() as session:
        runtime = await document_processing_settings.get_runtime_config(session)
        assert runtime.mineru_api_tokens == ()

    response = await client.delete(
        f"/api/admin/settings/document-processing/credentials/{credential_id}", headers=admin
    )
    assert response.status_code == 204
    async with get_sessionmaker()() as session:
        runtime = await document_processing_settings.get_runtime_config(session)
        assert runtime.mineru_api_tokens == ()


async def test_mineru_credential_probe_records_health_without_returning_secret(client, monkeypatch):
    admin, _member = await _admin_and_member(client)
    secret = "probe-secret-sentinel"
    response = await client.post(
        "/api/admin/settings/document-processing/credentials",
        json={"secret": secret, "enabled": True},
        headers=admin,
    )
    credential_id = response.json()["id"]

    async def fake_probe(**kwargs):
        assert kwargs["secret"] == secret
        return {
            "provider": "mineru",
            "ok": True,
            "latency_ms": 12,
            "status_code": 404,
            "detail": "MinerU credential accepted",
        }

    monkeypatch.setattr(document_processing_settings, "probe_mineru_credential", fake_probe)
    response = await client.post(
        f"/api/admin/settings/document-processing/credentials/{credential_id}/test",
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert secret not in response.text

    response = await client.get("/api/admin/settings/document-processing", headers=admin)
    credential = response.json()["mineru_credentials"][0]
    assert credential["health"]["ok"] is True
    assert secret not in response.text


async def test_mineru_probe_treats_authenticated_not_found_as_success():
    observed = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization")
        observed["path"] = request.url.path
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await document_processing_settings.probe_mineru_credential(
            base_url="https://mineru.example/api/v4",
            secret="test-key",
            timeout_seconds=60,
            client=client,
        )

    assert result["ok"] is True
    assert result["status_code"] == 404
    assert observed == {
        "authorization": "Bearer test-key",
        "path": "/api/v4/extract-results/batch/polaris-connectivity-probe",
    }
